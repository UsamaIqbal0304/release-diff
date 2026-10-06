# release-diff

What changed between two renderings of the same site, whether anybody meant it
to. Point it at staging and production, give it a list of paths, and it reads
eleven fields on both sides and fails on anything that moved and was not
declared.

One Python file, standard library only — `urllib`, `re`, `gzip`. A tool that
asks someone to install a dependency before it can check their own release does
not get run.

```
./release-diff.py --old https://staging.example.com \
                  --new https://example.com --paths paths.txt

./release-diff.py --old <old> --new <new> --paths paths.txt \
                  --expect declared.txt

./release-diff.py --selftest
```

`paths.txt` is one path per line, `#` for comments. Exit status is 0 when
nothing moved that was not written down, 3 when something did, and 2 on a usage
error — so it drops into a deploy pipeline the way a linter does.

## Why this exists

Asked on 6 October 2026 what gets an outsourced developer dropped, an SEO
agency owner answered that it is almost never bad code. It is silent changes: a
URL pattern that moved, a dropped canonical, an `<h1>` turned into a styled
`div` because it sat better in the layout, a noindex that never came off, a
redirect only ever tested on staging, an analytics tag that stopped firing.

The site works perfectly afterwards. That is the problem. Rankings slide three
weeks later, nobody connects it to the release, and it costs a client
relationship rather than a bug ticket.

Every one of those is a difference between two renderings of the same page that
a person looking at the page cannot see. **So the instrument is a diff, not a
checklist** — and that distinction is the whole point. The same thread put it
better than we would have: a checklist only catches what somebody already
thought to write down, and nobody writes down *"did the internal links end up
underneath a lazy-loaded block"*, because you would have to already suspect it.

A diff has no such ceiling. It reports what moved, including the thing nobody
anticipated.

## What it reads, per path, from both sides

| field | what it is |
| --- | --- |
| `status` | the HTTP status, after redirects |
| `redirect` | the chain of locations, by path |
| `title` | `<title>`, whitespace-flattened |
| `h1` | the first `<h1>`'s text, read through inline markup |
| `has_h1` | whether the page has one at all |
| `canonical` | `<link rel=canonical>` |
| `robots` | `<meta name=robots>` **plus** the `X-Robots-Tag` header, merged |
| `description` | `<meta name=description>` |
| `links` | the set of same-host link targets, as paths |
| `tags` | which measurement scripts are present, by vendor |
| `jsonld` | the set of `@type` values in clear-text JSON-LD |

`has_h1` is a separate field from `h1` on purpose. The failure the buyer named
is an h1 that *stopped being* an h1, and if the text survives into a `div` then
comparing h1 text alone reports nothing.

`robots` merges the markup and the header because either one can carry a
noindex and only one of them is in the HTML. A release that noindexes a site
through a header, with clean markup, is invisible to anything reading the page
source.

## Declaring a change

Most releases change something deliberately. Those go in `--expect`, one
`<path> <field>` per line:

```
/pricing/        title
/pricing/        description
/about/          links
```

That is the same shape as a ticket, which is the point: the file is a record of
what was meant. A declared change is still **printed** — it is only excluded
from the failure. Suppressing the print would hide the case where a declared
field moved in a way nobody intended.

## The host problem

This is the one real subtlety, and getting it wrong is how a release gate
becomes decoration.

A canonical on staging points at production, if staging is configured
correctly. A link graph on staging is written in staging's own host. Compare
those raw and the tool reports a difference on every page, every run — at which
point everyone turns it off.

So both sides' own hosts, and each side's view of the other's host, are
rewritten to a single token before comparing, and the two base hosts are never
themselves a finding. **A canonical pointing at a *third* host still is.**

## A gate that has never caught anything is decoration

Two proofs ship with the tool, because a diff run over two identical trees
reports nothing and proves nothing — the trees are identical.

**`--selftest`** runs the parsers against bytes held in the file: seventeen
checks on the four readings that are easy to get wrong and silent when they
are. An h1 that became a div, a canonical that differs only by host, a
measurement id inside a script the visible-copy strip removes, and whether a
chain of redirects has come back to a URL it already asked for.

```
$ ./release-diff.py --selftest
...
17 of 17
```

**`prove-it.py`** is the harder one. It serves a built site from disk twice over
loopback, applies each failure the agency owner named to one side on its own,
and asserts release-diff names the field that moved:

```
$ ./prove-it.py --tree /path/to/your/built/site
ok   control: a tree against itself reports nothing changed
ok   a canonical is dropped             -> canonical
ok   an h1 becomes a styled div         -> has_h1
ok   a noindex never came off           -> robots
ok   a url pattern moved                -> links
ok   a title is rewritten in the layout -> title
ok   an analytics tag stopped firing    -> tags
ok   the live url stops answering       -> status
ok   a path redirects to itself         -> BROKEN

8 of 8 failures caught
```

The control run matters as much as the eight: if a tree against itself reports
anything, none of the lines under it mean what they say.

It picks the page to break out of your tree rather than having a path baked
into it, and takes the most-used link prefix on that page as the pattern to
move, so it runs against a site it has never seen. `--page` overrides the
choice. Nothing touches a network: both sides are `http://127.0.0.1` on a port
the kernel picks, the mutated side is a throwaway copy, and your tree is only
read.

Two of the eight are worth a note, because they are honest substitutions rather
than the failure itself:

- *An analytics tag stopped firing* is tested in the other direction — the tag
  goes into the side standing in for "before", because a site with no tag has
  none to remove. That is the same difference.
- *A redirect tested on staging and never checked against the live box* is a
  rewrite rule a static server cannot hold. From outside it has one visible
  shape: the live URL stops answering. That is the only part of the failure a
  reader of the two renderings can see, so it is what the gate is held to.

## The one thing it reports without comparing anything

A redirect that names its own URL in `Location` is not a redirect. The browser
follows it to its own limit and then shows the visitor nothing — and no amount
of comparing will surface it, because **it is the same nothing on both sides**.
Every field matches and the path reads `SAME`, which is the one shape a diff is
blind to by construction.

That is not hypothetical. Reading every URL one UK manufacturer's sitemap
submits, on 6 October 2026, found eleven of fifty-four answering `301` with
their own address in `Location`, one of them the privacy policy their own home
page links twice.

So a path whose chain comes back to a URL it already requested is reported
absolutely, per side, and fails the run on its own. `--expect` cannot declare
one, because no release is meant to contain one:

```
$ ./release-diff.py --old http://127.0.0.1:8001 --new http://127.0.0.1:8001 \
                    --paths loop.txt
SAME     /
BROKEN   /a-path-that-redirects-to-itself/
    redirect    LOOP       old: redirect loop: 301 to /a-path-that-redirects-to-itself/, which was already requested
    redirect    LOOP       new: redirect loop: 301 to /a-path-that-redirects-to-itself/, which was already requested

2 redirect loop(s)

2 path(s) read, 0 changed, 0 undeclared field change(s)
FAIL: a redirect loop shows the visitor nothing, and --expect cannot declare one - no release is meant to contain it.
```

Both sides are the same host there on purpose. A loop on one side only moves
the `status` field, and an ordinary diff catches it; the symmetrical case is
the one that needed its own reading. `prove-it.py` holds the tool to it.

## Limits, stated rather than discovered later

It reads the HTML the server sends. A change made by JavaScript after load is
invisible to it, so a tag present in markup that fails to fire still reads as
present — the field is *"the snippet is on the page"*, not *"the measurement
arrived"*.

Two renderings taken minutes apart can differ for honest reasons on a page with
a date or a counter in it. That is what `--expect` is for.

It requests only the paths you give it, honours `robots.txt` on both hosts, and
writes nothing anywhere. Nothing is weighted, nothing is scored, and there is no
pass mark — a field either moved or it did not.

## Licence

MIT. See [LICENSE](LICENSE).

Written at [Plantroom Labs](https://plantroomlabs.com). The tool has a page
with a real release of that site run through it, FAIL and PASS both:
[plantroomlabs.com/tools/release-diff/](https://plantroomlabs.com/tools/release-diff/).
