"""IEEE — ieeexplore.ieee.org.

IEEE Xplore needs two hops, and the obvious one-hop URLs do not work.

The article toolbar's "Download PDF" control points at
`/stamp/stamp.jsp?tp=&arnumber={N}`, but that path renders an HTML
*viewer* page rather than the file: measured live, it returns
`text/html` titled "IEEE Xplore Full-Text PDF:" whose only useful
content is an iframe pointing one level deeper at
`/stampPDF/getPDF.jsp?tp=&arnumber={N}&ref=`. Navigating to *that*
fires the download event. So `stamp.jsp` is the link the page gives
you and `getPDF.jsp` is the one that yields bytes.

The article number is not derivable from the DOI, but it is in the
landing URL that `https://doi.org/{doi}` redirects to
(`ieeexplore.ieee.org/document/7583707`), so a single redirect gets it
without parsing the page.

Flow:
  1. Navigate `https://doi.org/{doi}` — redirects to the document page.
  2. Read the article number out of the landing URL, falling back to
     the `arnumber=` query parameter on the toolbar's PDF anchor for
     the layouts that land somewhere other than `/document/{N}`.
  3. Navigate `/stampPDF/getPDF.jsp?tp=&arnumber={N}&ref=` inside
     `expect_download()`.
  4. Fall back to `/stamp/stamp.jsp` and read the iframe `src`, for
     the case where step 3's URL shape changes but the viewer's does
     not.

Access is IP-based for a subscribing institution and was silent in
testing — no Cloudflare interstitial and no sign-in — hence
`needs_interactive_solve = False`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    Counter,
    PublisherHandler,
    cache_path_for,
    is_cached,
    progress_tag,
)

#: `/document/7583707`, `/abstract/document/7583707/`, `/document/7583707/`.
_ARNUMBER_IN_PATH = re.compile(r"/document/(\d+)")
#: `?...arnumber=7583707&...` on the toolbar's PDF anchor.
_ARNUMBER_IN_QUERY = re.compile(r"[?&]arnumber=(\d+)")


def _arnumber(url: str) -> str:
    """Article number from an IEEE Xplore URL, or "" if absent."""
    for pattern in (_ARNUMBER_IN_PATH, _ARNUMBER_IN_QUERY):
        m = pattern.search(url or "")
        if m:
            return m.group(1)
    return ""


class IeeeHandler(PublisherHandler):
    name = "ieee"
    display_name = "IEEE Xplore"
    doi_prefixes = ("10.1109/",)
    url_template = "https://doi.org/{doi}"
    direct_access_domains = ("ieeexplore.ieee.org", "ieee.org")
    concurrency = 1
    delay_s = 1.0
    # Measured 2026-08-23 against a cold profile on an institutional IP:
    # the document page and both PDF hops loaded with no challenge and no
    # sign-in. Raise this to True if a run starts reporting a wall.
    needs_interactive_solve = False

    setup_hint = (
        "IEEE Xplore authenticates by IP. On a subscribing network the\n"
        "article page shows a 'Download PDF' button in the toolbar and\n"
        "no sign-in is needed. If you see 'Sign In' or a purchase price\n"
        "instead, IEEE is not reachable from this session — check the\n"
        "VPN before letting the run continue."
    )

    async def download(
        self, page, ctx, item, cache_dir,
        *, counter: Counter, total: int, t_start: float,
    ) -> tuple[Path, str] | None:
        del ctx
        doi = item["doi"]
        out = cache_path_for(cache_dir, doi)
        if is_cached(out):
            counter.cached += 1
            return out, f"cache://{out}"

        url = self.url_template.format(doi=doi)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)

            arnumber = _arnumber(page.url)
            if not arnumber:
                # Landed somewhere without the id in the path; the
                # toolbar anchor carries it as a query parameter.
                href = await page.evaluate("""
                    () => {
                        const a = document.querySelector(
                            "a[href*='stamp.jsp'], a.xpl-btn-pdf");
                        return a ? a.href : "";
                    }
                """)
                arnumber = _arnumber(href)
            if not arnumber:
                raise RuntimeError(
                    f"No IEEE article number in {page.url} or in any PDF "
                    f"anchor — the DOI may not resolve to an Xplore document"
                )

            dl = None
            candidates = [
                f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp"
                f"?tp=&arnumber={arnumber}&ref=",
            ]
            for cand in candidates:
                try:
                    async with page.expect_download(timeout=25000) as dl_info:
                        try:
                            await page.goto(cand, wait_until="commit",
                                            timeout=15000)
                        except Exception:
                            pass  # download interrupts navigation
                    dl = await dl_info.value
                    break
                except Exception:
                    continue

            if dl is None:
                # The viewer page is the stable surface even when the
                # direct getPDF URL shape moves; its iframe names the
                # real target.
                stamp = (f"https://ieeexplore.ieee.org/stamp/stamp.jsp"
                         f"?tp=&arnumber={arnumber}")
                await page.goto(stamp, wait_until="domcontentloaded",
                                timeout=30000)
                await page.wait_for_timeout(1500)
                framed = await page.evaluate("""
                    () => {
                        const f = document.querySelector("iframe, frame");
                        return f ? f.src : "";
                    }
                """)
                if framed:
                    print(f"    viewer iframe: {framed}", flush=True)
                    async with page.expect_download(timeout=25000) as dl_info:
                        try:
                            await page.goto(framed, wait_until="commit",
                                            timeout=15000)
                        except Exception:
                            pass
                    dl = await dl_info.value

            if dl is None:
                raise RuntimeError(
                    "Neither getPDF.jsp nor the stamp.jsp viewer iframe "
                    "produced a download — usually means no access from "
                    "the current session"
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            await dl.save_as(str(out))
        except Exception as e:
            await self.report_failure(
                e, counter=counter, total=total, t_start=t_start,
                page=page, cache_dir=cache_dir, doi=doi,
            )
            return None

        if not is_cached(out):
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
            counter.failed += 1
            title = (item.get("title") or "")[:45]
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"not a PDF {title}",
                flush=True,
            )
            return None

        counter.ok += 1
        size = out.stat().st_size
        title = (item.get("title") or "")[:50]
        print(
            f"  {progress_tag(counter, total, t_start)} "
            f"ok ({size // 1024}KB) {title}",
            flush=True,
        )
        return out, url
