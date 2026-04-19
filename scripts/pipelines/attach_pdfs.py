#!/usr/bin/env python3
"""
Download full-text PDFs and attach them to Zotero items.

Sources (in order):
  1. Elsevier API (for Elsevier DOIs)
  2. SpringerLink (for Springer Nature DOIs — requires institutional network/VPN)
  3. Crossref TDM (publisher full-text via HTTP/1.1 — bypasses Cloudflare bot challenges)
  4. PubMed Central (free OA PDFs with proof-of-work challenge solver)
  5. OpenAlex Content API (paid, $0.01/download — broad coverage)
  6. Unpaywall (free OA copies)
  7. OpenAlex OA metadata (free OA URLs from OpenAlex)

Workflow:
  1. Fetch all journal articles from Zotero (local client for speed)
  2. Skip items that already have a PDF attachment
  3. Try each source in order until a PDF is found (parallel downloads)
  4. Upload PDF to Zotero as a child attachment (serial uploads)
  5. Log results to pdf_attach_log.csv

Required environment variables:
  ZOTERO_API_KEY    — Zotero API key
  ZOTERO_GROUP      — Zotero group ID
  CROSSREF_MAILTO   — Email for Crossref polite pool and TDM token
  ELSEVIER_API_KEY  — Elsevier API key (optional, for Elsevier DOIs)
  OPENALEX_API_KEY  — OpenAlex Content API key (optional, for paid PDFs)

Usage:
  python3 attach_pdfs.py                                        # full run
  python3 attach_pdfs.py --dry-run                             # check only
  python3 attach_pdfs.py --log-csv output/pdf_log.csv          # custom log
  python3 attach_pdfs.py --cache-dir output/pdf_cache          # custom cache
  python3 attach_pdfs.py --workers 4                           # fewer threads
"""

import argparse
import csv
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from http.cookiejar import Cookie

from pyzotero import zotero

# ---------------------------------------------------------------------------
# Configuration (from environment variables)
# ---------------------------------------------------------------------------
ZOTERO_API_KEY   = os.environ.get("ZOTERO_API_KEY", "")
ZOTERO_GROUP     = os.environ.get("ZOTERO_GROUP", "")
ELSEVIER_API_KEY = os.environ.get("ELSEVIER_API_KEY", "")
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "")
CROSSREF_MAILTO  = os.environ.get("CROSSREF_MAILTO", "")

ZOTERO_BASE    = f"https://api.zotero.org/groups/{ZOTERO_GROUP}"
ELSEVIER_BASE  = "https://api.elsevier.com/content/article/doi"

# Defaults (overridable via CLI)
DEFAULT_LOG_CSV   = os.path.join("output", "pdf_attach_log.csv")
DEFAULT_CACHE_DIR = os.path.join("output", "pdf_cache")

# Module-level cache dir — set in main() from args
PDF_CACHE_DIR = DEFAULT_CACHE_DIR

# ---------------------------------------------------------------------------
# SSL
# ---------------------------------------------------------------------------
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def http_get(url: str, headers: dict = None, timeout: int = 30) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return 0, b"", {}


def http_post(url: str, data: bytes, headers: dict = None, timeout: int = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return 0, b""


# ---------------------------------------------------------------------------
# Zotero clients: local for reads, remote for writes
# ---------------------------------------------------------------------------
def make_local_client() -> zotero.Zotero:
    return zotero.Zotero(ZOTERO_GROUP, "group", ZOTERO_API_KEY, local=True)


def make_remote_client() -> zotero.Zotero:
    return zotero.Zotero(ZOTERO_GROUP, "group", ZOTERO_API_KEY)


def get_all_items(local: zotero.Zotero) -> list[dict]:
    return local.everything(local.items(itemType="journalArticle"))


def get_pdf_map(local: zotero.Zotero) -> dict[str, tuple[bool, list[str]]]:
    """Bulk-fetch all PDF attachments and group by parent.

    Returns {parent_key: (has_real_file, [stub_keys])}.
    """
    all_att = local.everything(local.items(itemType="attachment"))
    pdfs = [a for a in all_att
            if a["data"].get("contentType") == "application/pdf"
            and a["data"].get("parentItem")]

    by_parent: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    for pdf in pdfs:
        parent = pdf["data"]["parentItem"]
        if pdf["data"].get("md5"):
            by_parent[parent][0].append(pdf)
        else:
            by_parent[parent][1].append(pdf)

    return {k: (bool(real), [s["data"]["key"] for s in stubs])
            for k, (real, stubs) in by_parent.items()}


def delete_item(item_key: str) -> None:
    """Delete a Zotero item (used to remove empty stubs)."""
    url = f"{ZOTERO_BASE}/items/{item_key}"
    req = urllib.request.Request(url, method="DELETE", headers={
        "Zotero-API-Key": ZOTERO_API_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL):
            pass
    except Exception:
        pass


def create_attachment_item(parent_key: str, filename: str) -> str | None:
    """Create a child attachment item and return its key."""
    url = f"{ZOTERO_BASE}/items"
    payload = json.dumps([{
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_file",
        "title": filename,
        "contentType": "application/pdf",
        "filename": filename,
        "charset": "",
    }]).encode()
    status, body = http_post(url, data=payload, headers={
        "Zotero-API-Key": ZOTERO_API_KEY,
        "Content-Type": "application/json",
    })
    if status == 200:
        data = json.loads(body)
        successful = data.get("successful", {})
        if "0" in successful:
            return successful["0"]["key"]
        failed = data.get("failed", {})
        print(f"    Create attachment failed: {failed}", file=sys.stderr)
        return None
    if status == 429:
        print("    Rate limited — sleeping 30s", file=sys.stderr)
        time.sleep(30)
        return create_attachment_item(parent_key, filename)  # retry once
    print(f"    Create attachment failed: HTTP {status} — {body[:200]}", file=sys.stderr)
    return None


def upload_pdf(attachment_key: str, pdf_bytes: bytes, filename: str) -> bool:
    """Upload PDF bytes to Zotero storage (3-step: authorize → S3 upload → register)."""
    md5 = hashlib.md5(pdf_bytes).hexdigest()
    mtime = int(time.time() * 1000)
    filesize = len(pdf_bytes)

    # Step 1: authorize upload
    auth_url = f"{ZOTERO_BASE}/items/{attachment_key}/file"
    auth_body = urllib.parse.urlencode({
        "md5": md5,
        "filename": filename,
        "filesize": filesize,
        "mtime": mtime,
    }).encode()
    status, body = http_post(auth_url, data=auth_body, headers={
        "Zotero-API-Key": ZOTERO_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
        "If-None-Match": "*",
    })
    if status not in (200, 412):
        print(f"    Auth failed: HTTP {status}", file=sys.stderr)
        return False

    auth = json.loads(body)

    if auth.get("exists") == 1:
        return True  # already uploaded

    # Step 2: upload to S3
    upload_url = auth["url"]
    s3_data = auth["prefix"].encode() + pdf_bytes + auth["suffix"].encode()
    s3_status, _ = http_post(upload_url, data=s3_data, headers={
        "Content-Type": auth["contentType"],
    }, timeout=60)
    if s3_status not in (200, 201, 204):
        print(f"    S3 upload failed: HTTP {s3_status}", file=sys.stderr)
        return False

    # Step 3: register upload
    reg_url = f"{ZOTERO_BASE}/items/{attachment_key}/file"
    reg_status, reg_body = http_post(
        reg_url,
        data=f"upload={auth['uploadKey']}".encode(),
        headers={
            "Zotero-API-Key": ZOTERO_API_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
            "If-None-Match": "*",
        },
    )
    if reg_status not in (200, 204):
        print(f"    Register failed: HTTP {reg_status} — {reg_body[:100]}", file=sys.stderr)
        return False

    return True


# ---------------------------------------------------------------------------
# PDF cache helper
# ---------------------------------------------------------------------------
def cache_path(doi: str) -> str:
    safe = doi.replace("/", "_").replace(":", "_")
    return os.path.join(PDF_CACHE_DIR, safe + ".pdf")


# ---------------------------------------------------------------------------
# PDF source functions
# ---------------------------------------------------------------------------
def fetch_elsevier_pdf(doi: str) -> bytes | None:
    if not ELSEVIER_API_KEY:
        return None
    path = cache_path(doi)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    url = f"{ELSEVIER_BASE}/{urllib.parse.quote(doi, safe='')}"
    status, body, headers = http_get(url, headers={
        "X-ELS-APIKey": ELSEVIER_API_KEY,
        "Accept": "application/pdf",
    }, timeout=30)
    if status == 200 and body[:4] == b"%PDF":
        with open(path, "wb") as f:
            f.write(body)
        return body
    return None


def fetch_springer_pdf(doi: str) -> bytes | None:
    """Download PDF directly from SpringerLink.

    Works for Springer Nature DOIs (10.1007/, 10.1057/, 10.1038/, etc.)
    when on a network with institutional access (e.g. FinELib).
    """
    SPRINGER_PREFIXES = ("10.1007/", "10.1057/", "10.1038/", "10.1140/",
                         "10.1186/", "10.1365/", "10.1245/")
    if not any(doi.startswith(p) for p in SPRINGER_PREFIXES):
        return None
    path = cache_path(doi)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    url = f"https://link.springer.com/content/pdf/{urllib.parse.quote(doi, safe='')}.pdf"
    status, body, _ = http_get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if status == 200 and body[:4] == b"%PDF":
        with open(path, "wb") as f:
            f.write(body)
        return body
    return None


def fetch_crossref_tdm_pdf(doi: str) -> bytes | None:
    """Download PDF via Crossref TDM (text-and-data-mining) links.

    Queries Crossref for publisher-registered TDM URLs and downloads the PDF.
    urllib uses HTTP/1.1 by default, which can bypass Cloudflare bot challenges
    on some publisher sites (though this is not always reliable).
    """
    if not CROSSREF_MAILTO:
        return None
    path = cache_path(doi)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    encoded = urllib.parse.quote(doi, safe="")
    cr_url = f"https://api.crossref.org/works/{encoded}?mailto={CROSSREF_MAILTO}"
    status, body, _ = http_get(cr_url)
    if status != 200:
        return None
    data = json.loads(body)
    links = data.get("message", {}).get("link", [])

    pdf_url = None
    for link in links:
        if (link.get("intended-application") == "text-mining"
                and link.get("content-type") == "application/pdf"):
            pdf_url = link["URL"]
            break
    if not pdf_url:
        return None

    req = urllib.request.Request(pdf_url, headers={
        "CR-Clickthrough-Client-Token": CROSSREF_MAILTO,
        "Accept": "application/pdf",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL) as resp:
            pdf = resp.read()
        if pdf[:4] != b"%PDF":
            return None
        with open(path, "wb") as f:
            f.write(pdf)
        return pdf
    except Exception:
        return None


def _pmc_solve_pow(challenge: str, difficulty: int) -> int:
    """Find nonce where SHA-256(challenge + str(nonce)) has `difficulty` leading hex zeros."""
    prefix = "0" * difficulty
    nonce = 0
    while True:
        h = hashlib.sha256((challenge + str(nonce)).encode()).hexdigest()
        if h.startswith(prefix):
            return nonce
        nonce += 1


def fetch_pmc_pdf(doi: str) -> bytes | None:
    """Download PDF from PubMed Central for Open Access articles.

    PMC serves a proof-of-work JavaScript challenge before delivering PDFs.
    This function solves the SHA-256 PoW, sets the resulting cookie, and
    re-requests the PDF.
    """
    path = cache_path(doi)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    # Step 1: Resolve DOI to PMC ID via NCBI ID converter
    conv_url = (
        "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        f"?ids={urllib.parse.quote(doi, safe='')}&format=json"
    )
    status, body, _ = http_get(conv_url)
    if status != 200:
        return None
    records = json.loads(body).get("records", [])
    if not records or "pmcid" not in records[0]:
        return None
    pmcid = records[0]["pmcid"]

    # Step 2: Request PDF URL — will return PoW challenge HTML
    pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=_SSL),
    )
    req = urllib.request.Request(pdf_url, headers={"User-Agent": UA})
    try:
        resp = opener.open(req, timeout=30)
        data = resp.read()
    except Exception:
        return None

    # If we got a PDF directly (no challenge), return it
    if data[:4] == b"%PDF":
        with open(path, "wb") as f:
            f.write(data)
        return data

    # Step 3: Parse PoW challenge from HTML
    html = data.decode(errors="replace")
    m_challenge = re.search(r'POW_CHALLENGE\s*=\s*"([^"]+)"', html)
    m_diff = re.search(r'POW_DIFFICULTY\s*=\s*"(\d+)"', html)
    m_name = re.search(r'POW_COOKIE_NAME\s*=\s*"([^"]+)"', html)
    m_path = re.search(r'POW_COOKIE_PATH\s*=\s*"([^"]+)"', html)
    if not all([m_challenge, m_diff, m_name, m_path]):
        return None

    challenge = m_challenge.group(1)
    difficulty = int(m_diff.group(1))
    cookie_name = m_name.group(1)
    cookie_path = m_path.group(1)

    # Step 4: Solve PoW
    nonce = _pmc_solve_pow(challenge, difficulty)

    # Step 5: Set cookie (format: "challenge,nonce") and re-request
    cookie_value = f"{challenge},{nonce}"
    c = Cookie(
        version=0, name=cookie_name, value=cookie_value,
        port=None, port_specified=False,
        domain=".ncbi.nlm.nih.gov", domain_specified=True,
        domain_initial_dot=True,
        path=cookie_path, path_specified=True,
        secure=True, expires=int(time.time()) + 18000,
        discard=False, comment=None, comment_url=None,
        rest={}, rfc2109=False,
    )
    cj.set_cookie(c)

    req2 = urllib.request.Request(pdf_url, headers={"User-Agent": UA})
    try:
        resp2 = opener.open(req2, timeout=60)
        pdf = resp2.read()
    except Exception:
        return None

    if pdf[:4] != b"%PDF":
        return None
    with open(path, "wb") as f:
        f.write(pdf)
    return pdf


def is_elsevier_doi(doi: str) -> bool:
    ELSEVIER_PREFIXES = (
        "10.1016/", "10.1006/", "10.1053/", "10.1054/",
        "10.1067/", "10.1074/", "10.1078/", "10.1383/",
    )
    return any(doi.startswith(p) for p in ELSEVIER_PREFIXES)


def fetch_pdf_from_url(url: str) -> bytes | None:
    """Download a PDF from any URL, return bytes or None."""
    ua = f"mailto:{CROSSREF_MAILTO}" if CROSSREF_MAILTO else "Mozilla/5.0"
    status, body, _ = http_get(url, headers={"User-Agent": ua}, timeout=30)
    if status == 200 and body[:4] == b"%PDF":
        return body
    return None


def fetch_unpaywall_pdf(doi: str) -> bytes | None:
    """Look up an open-access PDF URL via Unpaywall, then download it."""
    if not CROSSREF_MAILTO:
        return None
    path = cache_path(doi)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}?email={CROSSREF_MAILTO}"
    status, body, _ = http_get(url)
    if status != 200:
        return None
    data = json.loads(body)
    pdf_url = None
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or best.get("url")
    if not pdf_url:
        for loc in (data.get("oa_locations") or []):
            if loc.get("url_for_pdf"):
                pdf_url = loc["url_for_pdf"]
                break
    if not pdf_url:
        return None
    pdf = fetch_pdf_from_url(pdf_url)
    if pdf:
        with open(path, "wb") as f:
            f.write(pdf)
    return pdf


def fetch_openalex_content_pdf(doi: str) -> bytes | None:
    """Download PDF via OpenAlex Content API (paid, $0.01/download)."""
    if not OPENALEX_API_KEY:
        return None
    path = cache_path(doi)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    encoded = urllib.parse.quote(doi, safe="")
    url = f"https://api.openalex.org/works/doi:{encoded}?select=id,has_content&api_key={OPENALEX_API_KEY}"
    status, body, _ = http_get(url)
    if status != 200:
        return None
    data = json.loads(body)
    if not (data.get("has_content") or {}).get("pdf", False):
        return None
    work_id = data["id"].rsplit("/", 1)[-1]
    dl_url = f"https://content.openalex.org/works/{work_id}.pdf?api_key={OPENALEX_API_KEY}"
    req = urllib.request.Request(dl_url)
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL) as resp:
            pdf = resp.read()
        if pdf[:4] != b"%PDF":
            return None
        with open(path, "wb") as f:
            f.write(pdf)
        return pdf
    except Exception:
        return None


def fetch_openalex_pdf(doi: str) -> bytes | None:
    """Get the OA PDF URL from OpenAlex metadata, then download it."""
    path = cache_path(doi)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    mailto = CROSSREF_MAILTO or "user@example.com"
    encoded = urllib.parse.quote(f"https://doi.org/{doi}", safe="")
    url = f"https://api.openalex.org/works/{encoded}?mailto={mailto}"
    status, body, _ = http_get(url)
    if status != 200:
        return None
    data = json.loads(body)
    pdf_url = (data.get("open_access") or {}).get("oa_url")
    if not pdf_url:
        return None
    pdf = fetch_pdf_from_url(pdf_url)
    if pdf:
        with open(path, "wb") as f:
            f.write(pdf)
    return pdf


# ---------------------------------------------------------------------------
# CSV log
# ---------------------------------------------------------------------------
LOG_FIELDS = ["run_date", "item_key", "doi", "title", "status"]


def open_log(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    is_new = not os.path.exists(path)
    fh = open(path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
    if is_new:
        writer.writeheader()
    return fh, writer


def load_done_dois(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {r["doi"].strip().lower() for r in csv.DictReader(f)
                if r["status"] == "attached"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global PDF_CACHE_DIR

    parser = argparse.ArgumentParser(
        description="Download PDFs and attach them to Zotero items.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check availability only, do not upload")
    parser.add_argument("--log-csv", default=DEFAULT_LOG_CSV,
                        help=f"Path to log CSV (default: {DEFAULT_LOG_CSV})")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"PDF cache directory (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--workers", type=int, default=6,
                        help="Number of parallel download threads (default: 6)")
    parser.add_argument("--filter-keys-file",
                        help="Path to a text file with one Zotero item key per "
                             "line. Only those items are processed. Useful for "
                             "driving from a screening decision log.")
    args = parser.parse_args()

    # Validate required env vars
    if not ZOTERO_API_KEY:
        sys.exit("Error: ZOTERO_API_KEY environment variable not set.")
    if not ZOTERO_GROUP:
        sys.exit("Error: ZOTERO_GROUP environment variable not set.")

    PDF_CACHE_DIR = args.cache_dir
    os.makedirs(PDF_CACHE_DIR, exist_ok=True)

    run_date = date.today().isoformat()
    done_dois = load_done_dois(args.log_csv)

    # Local Zotero for fast reads
    print("Connecting to local Zotero client...", flush=True)
    local = make_local_client()

    print("Fetching Zotero items...", end=" ", flush=True)
    all_items = get_all_items(local)
    print(f"{len(all_items)} journal articles.", flush=True)

    # Optional: filter to items listed in --filter-keys-file
    if args.filter_keys_file:
        with open(args.filter_keys_file) as f:
            target_keys = {line.strip() for line in f if line.strip()}
        all_items = [it for it in all_items if it["key"] in target_keys]
        print(f"  After --filter-keys-file: {len(all_items)} items "
              f"(filter list had {len(target_keys)} keys)", flush=True)

    # Filter: not already processed
    candidates = [
        item for item in all_items
        if (doi := item["data"].get("DOI", "").strip())
        and doi.lower() not in done_dois
    ]
    print(f"Items not yet processed: {len(candidates)}", flush=True)

    # Bulk-fetch all PDF attachments via local API (fast)
    print("Checking for existing PDF attachments...", end=" ", flush=True)
    pdf_map = get_pdf_map(local)

    # Delete empty stubs and find items without real PDFs
    to_process = []
    stubs_deleted = 0
    for item in candidates:
        key = item["data"]["key"]
        has_file, stubs = pdf_map.get(key, (False, []))
        if stubs:
            for stub_key in stubs:
                delete_item(stub_key)
            stubs_deleted += len(stubs)
        if not has_file:
            to_process.append(item)
    print(f"{len(to_process)} items without real PDF"
          + (f" ({stubs_deleted} stubs deleted)" if stubs_deleted else "") + ".")

    log_fh, log_writer = open_log(args.log_csv)
    attached = no_pdf = failed = 0

    # --- Phase 1: Download PDFs in parallel ---
    def _fetch_one(item: dict) -> dict:
        """Try all sources for a single item. Returns result dict."""
        data = item["data"]
        doi = data.get("DOI", "").strip()
        title_full = data.get("title", "")

        pdf, source = None, ""
        if doi and is_elsevier_doi(doi):
            pdf = fetch_elsevier_pdf(doi)
            source = "elsevier"
        if not pdf and doi:
            pdf = fetch_springer_pdf(doi)
            source = "springer"
        if not pdf and doi:
            pdf = fetch_crossref_tdm_pdf(doi)
            source = "crossref_tdm"
        if not pdf and doi:
            pdf = fetch_pmc_pdf(doi)
            source = "pmc"
        if not pdf and doi:
            pdf = fetch_openalex_content_pdf(doi)
            source = "openalex_content"
        if not pdf and doi:
            pdf = fetch_unpaywall_pdf(doi)
            source = "unpaywall"
        if not pdf and doi:
            pdf = fetch_openalex_pdf(doi)
            source = "openalex_oa"

        return {
            "item": item,
            "key": data["key"],
            "doi": doi,
            "title": title_full[:70],
            "pdf": pdf,
            "source": source,
        }

    print(f"\n  Downloading PDFs ({args.workers} threads)...", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_fetch_one, item): item for item in to_process}
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            status_str = f"({r['source']}) {len(r['pdf'])//1024}KB" if r["pdf"] else "no PDF"
            print(f"  [{len(results)}/{len(to_process)}] {r['title']:<70} {status_str}")

    # --- Phase 2: Upload to Zotero (serial) ---
    found = [r for r in results if r["pdf"]]
    not_found = [r for r in results if not r["pdf"]]
    print(f"\n  Downloaded: {len(found)}, Not found: {len(not_found)}")

    for r in not_found:
        log_writer.writerow({"run_date": run_date, "item_key": r["key"],
                              "doi": r["doi"], "title": r["title"],
                              "status": "skipped_no_pdf"})
        no_pdf += 1

    if found and not args.dry_run:
        print(f"  Uploading {len(found)} PDFs to Zotero...\n")
    for j, r in enumerate(found, 1):
        key, doi, title = r["key"], r["doi"], r["title"]
        pdf, source = r["pdf"], r["source"]

        print(f"  [{j}/{len(found)}] {title:<70} ", end="", flush=True)
        print(f"({source}) {len(pdf)//1024}KB", end=" ", flush=True)

        if args.dry_run:
            print("[dry-run]")
            log_writer.writerow({"run_date": run_date, "item_key": key, "doi": doi,
                                  "title": title, "status": "dry_run"})
            attached += 1
            continue

        filename = (doi or title[:50]).replace("/", "_").replace(":", "_") + ".pdf"
        att_key = create_attachment_item(key, filename)
        if not att_key:
            print("→ create failed")
            log_writer.writerow({"run_date": run_date, "item_key": key, "doi": doi,
                                  "title": title, "status": "create_failed"})
            failed += 1
            time.sleep(0.5)
            continue

        ok = upload_pdf(att_key, pdf, filename)
        if ok:
            print("→ attached")
            log_writer.writerow({"run_date": run_date, "item_key": key, "doi": doi,
                                  "title": title, "status": "attached"})
            attached += 1
        else:
            print("→ upload failed")
            log_writer.writerow({"run_date": run_date, "item_key": key, "doi": doi,
                                  "title": title, "status": "upload_failed"})
            failed += 1

        time.sleep(0.3)

    log_fh.close()

    print(f"\n{'='*60}")
    print("Done.")
    print(f"  Attached:    {attached}")
    print(f"  No PDF:      {no_pdf}")
    print(f"  Failed:      {failed}")
    print(f"  Log:         {args.log_csv}")


if __name__ == "__main__":
    main()
