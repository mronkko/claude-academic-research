"""Oxford University Press — academic.oup.com.

OUP's PDF URL contains an opaque numeric article ID that isn't
derivable from the DOI, so we can't construct the URL directly.

Flow:
  1. Navigate `https://doi.org/{doi}` — redirects to academic.oup.com.
  2. Wait for the article toolbar to render, then extract the PDF
     anchor's href (selector: `a[href*='article-pdf'][href$='.pdf']`).
  3. Navigate to the extracted href; `plugins.always_open_pdf_externally`
     makes that navigation fire a download event. `ctx.request.get()`
     on the same URL returns 403 because CF rejects non-browser
     requests.

Ported verbatim from the working SLR-motivation script.
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    Counter,
    PublisherHandler,
    cache_path_for,
    is_cached,
    progress_tag,
)


class OupHandler(PublisherHandler):
    name = "oup"
    display_name = "Oxford University Press"
    doi_prefixes = ("10.1093/",)
    url_template = "https://doi.org/{doi}"
    direct_access_domains = ("academic.oup.com", "oup.com")
    concurrency = 1
    delay_s = 1.0

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
            await page.wait_for_timeout(2500)  # let the article toolbar render

            pdf_href = await page.evaluate("""
                () => {
                    const sel = "a[href*='article-pdf'][href*='.pdf'], "
                              + "a[href*='/pdf/'][href$='.pdf']";
                    const a = document.querySelector(sel);
                    return a ? a.href : null;
                }
            """)
            if not pdf_href:
                raise RuntimeError("PDF link not found on OUP landing page")

            async with page.expect_download(timeout=30000) as dl_info:
                try:
                    await page.goto(pdf_href, wait_until="commit", timeout=15000)
                except Exception:
                    pass  # download event interrupts navigation
            dl = await dl_info.value
            out.parent.mkdir(parents=True, exist_ok=True)
            await dl.save_as(str(out))
        except Exception as e:
            counter.failed += 1
            print(
                f"  {progress_tag(counter, total, t_start)} "
                f"ERROR: {str(e)[:100]}",
                flush=True,
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
        return out, pdf_href
