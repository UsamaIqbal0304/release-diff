#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Usama Iqbal (Plantroom Labs)
"""Break a copy of your own built site on purpose, and check release-diff says so.

    ./prove-it.py --tree /path/to/built/site
    ./prove-it.py --tree /path/to/built/site --page about/index.html

A diff run over two identical trees reports nothing and proves nothing,
because the trees are identical. So this serves your built site from disk
twice over loopback, applies each of the failures an agency owner named as the
reason an outsourced developer gets dropped to one side on its own, and
asserts release-diff names the field that moved.

Nothing here touches a network. Both sides are http://127.0.0.1 on a port the
kernel picks, the mutated side is a throwaway copy, and your tree is only
read.
"""
import argparse
import collections
import http.server
import io
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "release-diff.py")


def serve(root):
    """(httpd, base_url). The port is whatever the kernel gives.

    Two fixed ports made the second run of this fail to start: sockets from
    the first run were still in TIME-WAIT on them, and SO_REUSEADDR did not
    clear it. A test that only passes the first time is worse than no test,
    and the port number was never part of what is being measured.
    """
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=root, **k)

        def log_message(self, *a):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def run(old, new, paths):
    r = subprocess.run([sys.executable, TOOL, "--old", old, "--new", new,
                        "--paths", paths], capture_output=True, text=True)
    return r.returncode, r.stdout


def pick_page(tree):
    """A page with an <h1>, a <title>, a canonical and internal links.

    Chosen rather than named so this runs against a tree it has never seen.
    The deepest such document wins: a home page is usually the one page whose
    link graph is unlike every other, and an ordinary interior page is a
    fairer subject.
    """
    best = None
    for d, _, fs in os.walk(tree):
        for f in fs:
            if f != "index.html":
                continue
            p = os.path.join(d, f)
            s = io.open(p, encoding="utf-8", errors="replace").read()
            if not re.search(r"(?i)<h1[\s>]", s) or "<title" not in s.lower():
                continue
            if len(re.findall(r'href="/[^"]', s)) < 4:
                continue
            rel = os.path.relpath(p, tree)
            if best is None or rel.count(os.sep) > best.count(os.sep):
                best = rel
    return best


def link_prefix(src):
    """The most-used /segment/ in this page's own links, so "a url pattern
    moved" is a real pattern on a real page rather than a path we hoped for."""
    counts = collections.Counter(re.findall(r'href="(/[a-z0-9][a-z0-9-]*/)', src))
    return counts.most_common(1)[0][0] if counts else None


def failures(prefix):
    # (name, mutate, the field release-diff must name). None means the case is
    # handled by shape in main().
    out = [
        ("a canonical is dropped",
         lambda s: re.sub(r'(?i)<link rel="canonical"[^>]*>\n?', "", s, count=1),
         "canonical"),
        ("an h1 becomes a styled div",
         lambda s: re.sub(r"(?is)<h1([^>]*)>(.*?)</h1>",
                          r'<div class="h1"\1>\2</div>', s, count=1),
         "has_h1"),
        ("a noindex never came off",
         lambda s: s.replace("<head>",
                             '<head>\n<meta name="robots" content="noindex">', 1),
         "robots"),
        ("a title is rewritten in the layout",
         lambda s: re.sub(r"(?is)<title[^>]*>.*?</title>",
                          "<title>Rewritten by the layout</title>", s, count=1),
         "title"),
        ("an analytics tag stopped firing", None, "tags"),
        # "a redirect tested on staging and never checked against the live box"
        # is a rewrite rule a static server cannot hold, but from outside it
        # has one visible shape: the live URL stops answering. That is the only
        # part of the failure a reader of the two renderings can see, so it is
        # what the gate is held to.
        ("the live url stops answering", None, "status"),
    ]
    if prefix:
        out.insert(3, ("a url pattern moved",
                       lambda s: s.replace('href="%s' % prefix,
                                           'href="/moved%s' % prefix),
                       "links"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tree", required=True, help="a built site on disk")
    ap.add_argument("--page", help="relative path of the page to break "
                                   "(default: picked from the tree)")
    a = ap.parse_args()
    tree = os.path.abspath(os.path.expanduser(a.tree))
    if not os.path.isdir(tree):
        sys.exit("not a directory: " + tree)

    rel = a.page or pick_page(tree)
    if not rel:
        sys.exit("no page in %s has an h1, a title and four internal links - "
                 "pass --page" % tree)
    src = io.open(os.path.join(tree, rel), encoding="utf-8",
                  errors="replace").read()
    prefix = link_prefix(src)
    page_url = "/" + os.path.dirname(rel).replace(os.sep, "/")
    page_url = (page_url.rstrip("/") + "/") if page_url != "/" else "/"
    print("tree   %s\npage   %s  (%s)\nlinks  %s\n"
          % (tree, rel, page_url, prefix or "none found, that case is skipped"))

    work = tempfile.mkdtemp(prefix="release-diff-prove.")
    mut_tree = os.path.join(work, "mutated")
    paths = os.path.join(work, "paths.txt")
    io.open(paths, "w").write("/\n%s\n" % page_url)
    try:
        base, base_url = serve(tree)
        rc, out = run(base_url, base_url, paths)
        if rc != 0 or "0 changed" not in out:
            print(out)
            sys.exit("the control failed: this tree against itself is not "
                     "identical, so nothing below would mean anything")
        print("ok   control: a tree against itself reports nothing changed")

        mut, mut_url = serve(mut_tree)
        cases = failures(prefix)
        passed = 0
        for name, fn, field in cases:
            shutil.rmtree(mut_tree, ignore_errors=True)
            shutil.copytree(tree, mut_tree)
            p = os.path.join(mut_tree, rel)
            s = io.open(p, encoding="utf-8", errors="replace").read()
            if field == "status":
                # The whole directory, not just its index.html: removing the
                # file leaves the directory, and SimpleHTTPRequestHandler
                # answers a directory with a generated listing at 200. The
                # first version of this case removed the file and the status
                # never moved, so it read MISS for the test's own fault.
                shutil.rmtree(os.path.dirname(p))
            elif field == "tags":
                # A site with no measurement tag has nothing to remove, so the
                # tag goes into the side that stands in for "before" instead.
                # That is the same difference, in the other direction.
                s2 = s.replace("</head>",
                               "<script>var g='G-TEST12345';</script></head>", 1)
                if s2 == s:
                    print("MISS %-34s -> %s (no </head> to inject into)"
                          % (name, field))
                    continue
                io.open(p, "w", encoding="utf-8").write(s2)
            else:
                s2 = fn(s)
                if s2 == s:
                    print("MISS %-34s -> %s (nothing to break on this page)"
                          % (name, field))
                    continue
                io.open(p, "w", encoding="utf-8").write(s2)
            rc, out = run(base_url, mut_url, paths)
            hit = rc == 3 and re.search(r"^\s+%s\s+UNDECLARED" % field, out, re.M)
            print("%-4s %-34s -> %s" % ("ok" if hit else "MISS", name, field))
            if not hit:
                print(out)
            passed += bool(hit)
        base.shutdown()
        mut.shutdown()
        print("\n%d of %d failures caught" % (passed, len(cases)))
        return 0 if passed == len(cases) else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
