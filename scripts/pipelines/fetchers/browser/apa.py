"""APA PsycNET — psycnet.apa.org.

APA gates every article behind a multi-step click-through. Flow:

  1. Navigate `https://doi.org/{doi}` → `psycnet.apa.org/doiLanding?doi=...`.
  2. Click "Get Access" → overlay opens.  (Some users with an
     already-authenticated session are auto-routed past this step.)
  3. Click "Check Access" → navigates to
     `/recordAccess/institutional/{apaID}`.
  4. Click "Download PDF" — with `always_open_pdf_externally=true`
     on the Chromium profile, that click fires a real download event.

Institutional SSO cookies from step 1 persist across all subsequent
DOIs, so only the first item requires a login.

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
    try_click,
)


class ApaHandler(PublisherHandler):
    name = "apa"
    display_name = "APA PsycNET"
    doi_prefixes = ("10.1037/",)
    url_template = "https://doi.org/{doi}"
    direct_access_domains = ("psycnet.apa.org", "apa.org")
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
            # Step 1: doi.org → doiLanding
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)

            # Step 2: Click "Get Access" (opens overlay). Some users with
            # existing institutional access are auto-routed past this step.
            await try_click(
                page,
                "button:has-text('Get Access')",
                "a:has-text('Get Access')",
                "[data-action='get-access']",
                timeout=5000,
            )
            await page.wait_for_timeout(1000)

            # Step 3: Click "CHECK ACCESS" in the overlay.
            await try_click(
                page,
                "button:has-text('Check Access')",
                "a:has-text('Check Access')",
                "button:has-text('CHECK ACCESS')",
                timeout=8000,
            )
            # Wait for navigation to /recordAccess/institutional/...
            try:
                await page.wait_for_url(
                    "**/recordAccess/institutional/**", timeout=15000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            # Step 4: Click "DOWNLOAD PDF" — fires a download event because
            # the Chromium profile has the built-in viewer disabled.
            async with page.expect_download(timeout=30000) as dl_info:
                clicked = await try_click(
                    page,
                    "a[href*='/fulltext/'][href*='.pdf']",
                    "button:has-text('Download PDF')",
                    "a:has-text('Download PDF')",
                    "button:has-text('DOWNLOAD PDF')",
                    "a:has-text('Download')",
                    timeout=15000,
                )
                if not clicked:
                    raise RuntimeError("Download button not found")
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
        return out, url
