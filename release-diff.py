#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Usama Iqbal (Plantroom Labs)
"""What changed between two renderings of the same site, whether anybody meant it to.

    ./release-diff.py --old https://staging.example.com \
                      --new https://example.com --paths paths.txt
    ./release-diff.py --old <base> --new <base> --paths paths.txt \
                      --expect declared.txt
    ./release-diff.py --selftest

Why this exists. Asked on 6 Oct 2026 what gets an outsourced developer
dropped, an SEO agency owner answered that it is not bad code, it is silent
changes: a URL pattern that moved, a dropped canonical, an H1 turned into a
styled div because it sat better in the layout, a noindex that never came
off, a redirect only ever tested on staging, an analytics tag that stopped
firing. The site works perfectly afterwards. Rankings slide three weeks later
and nobody connects it to the release, and it costs a client relationship
rather than a bug ticket.

Every one of those is a difference between two renderings of the same page
that a person looking at the page cannot see. So the instrument is a diff, not
a checklist - and that distinction is the whole point. The same thread said it
better than we would have: a checklist only catches what somebody already
thought to write down, and nobody writes down "did the internal links end up
under a lazy loaded block", because you would have to already suspect it.

Hence: this compares two renderings on every field it can read, reports
everything that moved, and fails on anything that moved and was not declared.
A deliberate change is declared in --expect, one `<path> <field>` per line,
which is the same shape as a ticket. Nothing is weighted, nothing is scored,
and there is no pass mark.

What it reads per path, from both sides:

  status        the HTTP status, after redirects
  redirect      the chain of locations, by path
  title         <title>, whitespace-flattened
  h1            the first <h1>'s text - and HAS_H1 separately, because the
                failure the buyer named is an h1 that stopped being an h1
  canonical     <link rel=canonical>
  robots        <meta name=robots> plus the X-Robots-Tag header, merged,
                because either one can carry a noindex and only one of them
                is in the markup
  description   <meta name=description>
  links         the set of same-host link targets, as paths
  tags          which measurement scripts are present, by vendor
  jsonld        the set of @type values in clear-text JSON-LD

The host problem, which is the one real subtlety. A canonical on staging
points at production if staging is configured correctly, and a link graph on
staging is written in staging's own host. Comparing those raw reports a
difference on every page and the tool becomes noise, which is how a gate
becomes decoration. So both sides' own hosts - and the other side's host -
are rewritten to a single token before comparing, and the two base hosts are
never themselves a finding. A canonical pointing at a *third* host still is.

Limits, stated rather than discovered later. This reads the HTML the server
sends. A change made by JavaScript after load is invisible to it, so a tag
that is present in markup and fails to fire still reads as present - the
field is "the snippet is on the page", not "the measurement arrived". Two
renderings taken minutes apart can also differ for honest reasons on a page
with a date or a counter in it, and that is what --expect is for.
"""
import argparse
import gzip
import html
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib

UA = "plantroom-release-diff/1.0 (+https://plantroomlabs.com)"
TIMEOUT = 25
FIELDS = ("status", "redirect", "title", "h1", "has_h1", "canonical",
          "robots", "description", "links", "tags", "jsonld")
# The two fields whose value is a set rather than a sentence. They are
# reported as a difference: see the print loop in main().
SET_FIELDS = ("links", "tags")

# A measurement snippet is identified by the string its own vendor requires,
# not by a script filename, because the filename is what a bundler changes.
TAG_SIGNS = (
    ("ga4", r"\bG-[A-Z0-9]{6,}\b"),
    ("gtm", r"\bGTM-[A-Z0-9]{4,}\b"),
    ("universal-analytics", r"\bUA-\d{4,}-\d+\b"),
    ("google-ads", r"\bAW-\d{6,}\b"),
    ("plausible", r"plausible\.io/js"),
    ("matomo", r"matomo\.(?:js|php)"),
    ("fathom", r"cdn\.usefathom\.com"),
    ("clarity", r"clarity\.ms/tag"),
    ("hotjar", r"static\.hotjar\.com"),
    ("meta-pixel", r"connect\.facebook\.net/[^\"']*fbevents"),
    ("linkedin", r"snap\.licdn\.com"),
    ("segment", r"cdn\.segment\.com"),
    ("posthog", r"\bposthog\.(?:init|com)\b"),
)

_robots = {}


def allowed(url):
    """robots.txt for the host, cached. Fetched by hand rather than through
    RobotFileParser.read(), which turns any error into "everything is
    disallowed" - a 4xx on robots.txt is the absence of a policy, not a
    refusal. Same reading as tools/outreach/collect.py."""
    host = urllib.parse.urlsplit(url).netloc
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        robots_url = urllib.parse.urlunsplit(
            urllib.parse.urlsplit(url)[:2] + ("/robots.txt", "", ""))
        try:
            req = urllib.request.Request(robots_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
        except Exception:
            rp.parse([])
        _robots[host] = rp
    return _robots[host].can_fetch(UA, url)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects by hand, so the chain is evidence rather than a
    detail urllib swallowed. A redirect only ever tested on staging is one of
    the six failures this tool exists for, and it is invisible unless the
    hops are recorded."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, max_hops=6):
    """(status, final_url, chain, headers, body). Never raises for an HTTP
    status; a transport failure is returned as status 0 with the reason, so a
    host that refuses one side is a reported difference rather than a crash."""
    opener = urllib.request.build_opener(_NoRedirect)
    chain, cur = [], url
    for _ in range(max_hops + 1):
        if not allowed(cur):
            return 0, cur, chain, {}, "", "robots.txt disallows this path"
        req = urllib.request.Request(cur, headers={
            "User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        try:
            r = opener.open(req, timeout=TIMEOUT)
            status, hdrs, raw = r.getcode(), dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, raw = e.code, dict(e.headers), e.read()
        except Exception as e:
            return 0, cur, chain, {}, "", "%s: %s" % (type(e).__name__, e)
        if status in (301, 302, 303, 307, 308) and hdrs.get("Location"):
            nxt = urllib.parse.urljoin(cur, hdrs["Location"])
            chain.append((status, nxt))
            cur = nxt
            continue
        enc = (hdrs.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return status, cur, chain, hdrs, raw.decode("utf-8", "replace"), None
    return 0, cur, chain, {}, "", "more than %d redirects" % max_hops


def _flat(t):
    return re.sub(r"\s+", " ", html.unescape(t or "")).strip()


def _strip_code(body):
    return re.sub(r"(?is)<(script|style|template|noscript)\b[^>]*>.*?</\1>", " ", body)


def _attr(tag, name):
    m = re.search(r'(?i)\b%s\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))' % name, tag)
    if not m:
        return ""
    return _flat(m.group(1) or m.group(2) or m.group(3) or "")


def _meta(body, name_attr, name_val):
    for tag in re.findall(r"(?is)<meta\b[^>]*>", body):
        if _attr(tag, name_attr).lower() == name_val:
            return _attr(tag, "content")
    return ""


def read_page(url, status, final_url, chain, hdrs, body, hosts):
    """Every field, with both base hosts rewritten to one token."""
    def norm(u):
        u = urllib.parse.urljoin(final_url, u)
        p = urllib.parse.urlsplit(u)
        host = "SITE" if p.netloc.lower() in hosts else p.netloc.lower()
        return "%s://%s%s%s" % ("SCHEME", host, p.path or "/",
                                ("?" + p.query) if p.query else "")

    out = {"status": str(status)}
    out["redirect"] = " -> ".join("%d %s" % (c, norm(u)) for c, u in chain) or "-"
    if not body:
        for f in FIELDS[2:]:
            out[f] = "-"
        return out

    noc = _strip_code(body)
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    out["title"] = _flat(m.group(1)) if m else "-"
    m = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", noc)
    out["has_h1"] = "yes" if m else "no"
    out["h1"] = _flat(re.sub(r"<[^>]+>", " ", m.group(1))) if m else "-"

    can = ""
    for tag in re.findall(r"(?is)<link\b[^>]*>", body):
        if "canonical" in _attr(tag, "rel").lower():
            can = _attr(tag, "href")
            break
    out["canonical"] = norm(can) if can else "-"

    robots = [v for v in (_meta(body, "name", "robots"),
                          hdrs.get("X-Robots-Tag", "")) if v]
    out["robots"] = ", ".join(sorted(_flat(r).lower() for r in robots)) or "-"
    out["description"] = _meta(body, "name", "description") or "-"

    links = set()
    for tag in re.findall(r"(?is)<a\b[^>]*>", noc):
        href = _attr(tag, "href")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        n = norm(href)
        if "//SITE" in n:
            links.add(urllib.parse.urlsplit(n.replace("SCHEME://SITE", "x://x")).path)
    out["links"] = " ".join(sorted(links)) or "-"

    found = [n for n, pat in TAG_SIGNS if re.search(pat, body)]
    out["tags"] = " ".join(sorted(found)) or "-"
    out["jsonld"] = " ".join(sorted({
        _flat(t) for blk in re.findall(
            r'(?is)<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', body)
        for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', blk)})) or "-"
    return out


def load_expect(path):
    """One `<path> <field>` per line, # for a comment. A declared change is a
    change somebody decided to make, which is the only thing that separates a
    release from a regression."""
    declared = set()
    if not path:
        return declared
    for ln in io.open(path, encoding="utf-8"):
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        parts = ln.split()
        assert len(parts) == 2, "expect line is '<path> <field>': %r" % ln
        assert parts[1] in FIELDS, "unknown field %r (of %s)" % (parts[1], ", ".join(FIELDS))
        declared.add((parts[0], parts[1]))
    return declared


def diff_path(old_base, new_base, path, hosts):
    rows = []
    sides = {}
    for side, base in (("old", old_base), ("new", new_base)):
        url = urllib.parse.urljoin(base, path)
        st, fin, chain, hdrs, body, err = fetch(url)
        if err:
            sides[side] = {f: "-" for f in FIELDS}
            sides[side]["status"] = "ERR"
            sides[side]["redirect"] = err
        else:
            sides[side] = read_page(url, st, fin, chain, hdrs, body, hosts)
    for f in FIELDS:
        a, b = sides["old"].get(f, "-"), sides["new"].get(f, "-")
        if a != b:
            rows.append((f, a, b))
    return rows


def selftest():
    """The parsers, on bytes held here. Checks the three readings that are
    easy to get wrong and silent when they are: an h1 that became a div, a
    canonical that differs only by host, and a measurement id inside a script
    the visible-copy strip removes."""
    hosts = {"stage.example.com", "example.com"}
    page_a = """<html><head><title> A  page </title>
      <link rel="canonical" href="https://example.com/x/">
      <meta name="robots" content="index,follow">
      <script>var id='G-ABC123XYZ';</script></head>
      <body><h1>The <em>heading</em></h1>
      <a href="/b/">b</a><a href="https://other.test/c">c</a>
      <script>document.write('<a href="/never/">x</a>')</script></body></html>"""
    page_b = page_a.replace("<h1>The <em>heading</em></h1>",
                            '<div class="h1">The <em>heading</em></div>')
    a = read_page("https://stage.example.com/x/", 200, "https://stage.example.com/x/",
                  [], {}, page_a, hosts)
    b = read_page("https://example.com/x/", 200, "https://example.com/x/",
                  [], {}, page_b, hosts)
    checks = [
        ("title flattened", a["title"] == "A page"),
        ("h1 text read through inline markup", a["h1"] == "The heading"),
        ("has_h1 yes on the real heading", a["has_h1"] == "yes"),
        ("has_h1 no once it is a styled div", b["has_h1"] == "no"),
        ("h1 text gone with it", b["h1"] == "-"),
        ("canonical host folded to SITE", a["canonical"] == "SCHEME://SITE/x/"),
        ("canonical equal across the two hosts", a["canonical"] == b["canonical"]),
        ("ga4 id found inside a script", a["tags"] == "ga4"),
        ("internal link kept as a path", a["links"] == "/b/"),
        ("third-host link not counted internal", "other.test" not in a["links"]),
        ("link written by script not counted", "/never/" not in a["links"]),
        ("robots merged and lower-cased", a["robots"] == "index,follow"),
    ]
    hdr = read_page("https://example.com/y/", 200, "https://example.com/y/", [],
                    {"X-Robots-Tag": "noindex"}, "<html><head></head><body></body></html>",
                    hosts)
    checks.append(("X-Robots-Tag read as robots", hdr["robots"] == "noindex"))
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("%-4s %s" % ("ok" if ok else "FAIL", n))
    print("\n%d of %d" % (len(checks) - len(bad), len(checks)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", help="base URL of the rendering that is already live")
    ap.add_argument("--new", help="base URL of the rendering about to replace it")
    ap.add_argument("--paths", help="file of paths, one per line")
    ap.add_argument("--expect", help="declared changes, '<path> <field>' per line")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.old and a.new and a.paths):
        ap.error("--old, --new and --paths are all required")

    hosts = {urllib.parse.urlsplit(u).netloc.lower() for u in (a.old, a.new)}
    declared = load_expect(a.expect)
    paths = [l.strip() for l in io.open(a.paths, encoding="utf-8")
             if l.strip() and not l.startswith("#")]

    undeclared = 0
    changed_paths = 0
    for p in paths:
        rows = diff_path(a.old, a.new, p, hosts)
        if not rows:
            print("SAME     %s" % p)
            continue
        changed_paths += 1
        print("CHANGED  %s" % p)
        for f, old, new in rows:
            mark = "declared" if (p, f) in declared else "UNDECLARED"
            if mark == "UNDECLARED":
                undeclared += 1
            if f in SET_FIELDS:
                # A set of several hundred link paths printed as two truncated
                # strings is two strings that look the same. One added link is
                # the whole finding, so print the difference, not the sides.
                # Not `a` and `b`: `a` is the argparse namespace in this
                # scope, and shadowing it made the second path crash.
                was = set(old.split()) - {"-"}
                now = set(new.split()) - {"-"}
                for tok in sorted(now - was):
                    print("    %-11s %-10s added:   %s" % (f, mark, tok))
                    f = ""
                for tok in sorted(was - now):
                    print("    %-11s %-10s removed: %s" % (f, mark, tok))
                    f = ""
                continue
            print("    %-11s %-10s old: %s" % (f, mark, old[:300]))
            print("    %-11s %-10s new: %s" % ("", "", new[:300]))

    print("\n%d path(s) read, %d changed, %d undeclared field change(s)"
          % (len(paths), changed_paths, undeclared))
    if undeclared:
        print("FAIL: a release that changes these fields without declaring them is "
              "the failure this tool exists for.")
        return 3
    print("PASS: nothing moved that was not written down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
