"""INFORMS — pubsonline.informs.org.

INFORMS gates PDFs behind an access-options page (member login,
purchase, token, guest access). The "Download PDF" control on that
page is a button/link with session-bound JavaScript: navigating
directly to the constructed `/doi/pdfdirect/{doi}` URL silently
redirects back to the access page without firing a download, because
the session hasn't "claimed" access through the button interaction.

Flow:
  1. Navigate `https://doi.org/{doi}` — redirects to pubsonline.informs.org.
  2. Wait for the `Download PDF` control to appear (guest / token /
     signed-in readers all see this control; readers hitting a hard
     paywall will see `Purchase` and nothing to click).
  3. Click that control inside `page.expect_download()`. Clicking —
     not navigation — is what activates the access mechanism.
  4. Fall back to URL rewrite (`/doi/pdfdirect/` → `/doi/pdf/` → the
     original href) for readers whose institution's SSO makes the
     direct URL work without a click; observed to succeed for some
     access configurations.
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


class InformsHandler(PublisherHandler):
    name = "informs"
    display_name = "INFORMS"
    doi_prefixes = ("10.1287/",)
    url_template = "https://doi.org/{doi}"
    direct_access_domains = ("pubsonline.informs.org", "informs.org")
    concurrency = 1
    delay_s = 1.0

    setup_hint = (
        "INFORMS gates PDFs behind an access page showing member login,\n"
        "purchase, token access, and (for eligible readers) a 'Download\n"
        "PDF' button. The script clicks that button — which activates\n"
        "the session-bound download mechanism — so you don't need to\n"
        "click it manually. Just make sure that when the page loads,\n"
        "you can see 'Download PDF' somewhere (guest access, IP-based\n"
        "institutional access, or signed-in account). If you only see\n"
        "'Purchase $30.00' with no download option, INFORMS isn't\n"
        "accessible from this session."
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
            await page.wait_for_timeout(2000)

            # --- Attempt 1: click the "Download PDF" control ---
            # INFORMS's access page renders PDF access as a button/link
            # with session-bound JavaScript; clicking it activates the
            # download, whereas navigating to /doi/pdfdirect/ silently
            # redirects back to the access page.
            dl = None
            try:
                async with page.expect_download(timeout=15000) as dl_info:
                    clicked = await try_click(
                        page,
                        # Explicit text variants — INFORMS has used both
                        # upper- and title-cased versions.
                        "a:has-text('Download PDF')",
                        "button:has-text('Download PDF')",
                        "a:has-text('DOWNLOAD PDF')",
                        "button:has-text('DOWNLOAD PDF')",
                        # Fallback: any anchor whose href is a PDF route.
                        "a[href*='/doi/pdfdirect/']",
                        "a[href*='/doi/pdf/']",
                        "a[href*='/doi/epdf/']",
                        timeout=10000,
                    )
                    if not clicked:
                        raise RuntimeError("No 'Download PDF' control found")
                dl = await dl_info.value
                trigger_url = page.url
                print(
                    f"    clicked Download PDF → {trigger_url}",
                    flush=True,
                )
            except Exception as e:
                print(f"    click path failed: {str(e)[:80]}", flush=True)

            # --- Attempt 2: URL-rewrite fallback (for SSO sessions where
            # the direct URL does work) ---
            if dl is None:
                pdf_href = await page.evaluate("""
                    () => {
                        const sel = "a[href*='/doi/pdfdirect/'], "
                                  + "a[href*='/doi/pdfplus/'], "
                                  + "a[href*='/doi/pdf/'], "
                                  + "a[href*='/doi/epdf/']";
                        const a = document.querySelector(sel);
                        return a ? a.href : null;
                    }
                """)
                if pdf_href:
                    print(f"    found href: {pdf_href}", flush=True)
                    raw_candidates = [
                        pdf_href.replace("/doi/epdf/", "/doi/pdfdirect/")
                                 .replace("/doi/pdf/", "/doi/pdfdirect/"),
                        pdf_href.replace("/doi/epdf/", "/doi/pdf/"),
                        pdf_href,
                    ]
                    seen: set[str] = set()
                    candidates: list[str] = []
                    for c in raw_candidates:
                        if c not in seen:
                            seen.add(c)
                            candidates.append(c)
                    for cand in candidates:
                        print(f"    trying: {cand}", flush=True)
                        try:
                            async with page.expect_download(timeout=15000) as dl_info:
                                try:
                                    await page.goto(cand, wait_until="commit",
                                                    timeout=10000)
                                except Exception:
                                    pass  # download interrupts navigation
                            dl = await dl_info.value
                            break
                        except Exception:
                            continue

            if dl is None:
                # Diagnostic: save whatever the page currently shows.
                diag_dir = Path(cache_dir)
                diag_dir.mkdir(parents=True, exist_ok=True)
                diag_path = diag_dir / (
                    f"informs_nodownload_{doi.replace('/', '_')}.html"
                )
                try:
                    html = await page.content()
                    diag_path.write_text(html, encoding="utf-8")
                    print(
                        f"    saved current page HTML → {diag_path}",
                        flush=True,
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"Neither the 'Download PDF' click nor URL rewrite "
                    f"produced a download. See {diag_path} — usually "
                    f"means no access from the current session."
                )
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
