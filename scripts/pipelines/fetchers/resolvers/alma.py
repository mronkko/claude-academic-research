"""Ex Libris Alma `uresolver` — what Primo VE institutions actually have.

Primo's public OpenURL path redirects to the HTML discovery UI and is
useless for a machine, so the endpoint to talk to is Alma's
`https://<host>.alma.exlibrisgroup.com/view/uresolver/<inst_code>/openurl`.

Three ways it differs from SFX, all handled here:

1. **`svc_dat=CTO` is mandatory.** Without it Alma serves its HTML
   discovery skin with HTTP 200 — parseable-looking, unparseable in fact.
2. **The full-text marker is an attribute, not a child element.**
   `<context_service service_type="getFullTxt">`, with the resolvable
   link in a sibling `<resolution_url>` rather than SFX's `<target_url>`.
3. **The platform identity is not in the URL.** Every `resolution_url`
   points at the Alma redirector (`<tenant>.alma.exlibrisgroup.com`),
   so host-based ranking is blind. The names live in a `<keys>` child as
   `<key id="package_public_name">` / `<key id="interface_name">`, and
   extracting them is what lets the shared ranking in `base.py` prefer
   EBSCOhost over ProQuest on Alma at all. Measured live: a DOI with 15
   routes returned `interface_name` values of Springer Link, EBSCOhost,
   JSTOR and ProQuest, none of which is visible in any URL.

Date filtering does not work here. `supports_date_threshold` is False
because live testing across correct, wrong and absent `rft.date` /
`rft.volume` values returned identical results — Alma simply does not
filter on them, and `sfx.ignore_date_threshold` is an SFX parameter it
ignores. Do not assume one tenant's behaviour generalises; if a
deployment is found that *does* filter, this flag is the one thing to
flip.

No `sfx.*` parameters are sent. Verified live that their presence or
absence makes no difference to the result (15 services either way), so
they are omitted rather than carried as cargo.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from .base import (
    FULLTEXT_SERVICE_TYPE,
    OPENURL_CONTEXT_PARAMS,
    FulltextTarget,
    LibraryResolver,
    ResolverRequest,
    local_name,
)

logger = logging.getLogger(__name__)

# Alma's marker for "return the getFullTxt service category as XML".
_SVC_DAT = "CTO"

# `/view/uresolver/` is a fixed Alma product path, not something an
# institution configures, so detection needs no network round-trip.
_URESOLVER_PATH = "/view/uresolver/"

_TRUE_WORDS = ("true", "yes", "1")


class AlmaResolver(LibraryResolver):
    flavour = "alma"
    supports_date_threshold = False

    @classmethod
    def matches(cls, openurl_base: str) -> bool:
        return _URESOLVER_PATH in openurl_base

    def query_urls(self, req: ResolverRequest) -> list[str]:
        """DOI-keyed query, then an ISSN-keyed fallback when possible.

        The fallback is a genuinely different question, not a variant:
        some Alma deployments link holdings only at journal level and
        return nothing for a DOI-keyed query even when they license the
        journal. Asking by ISSN (plus date/volume when known) recovers
        those. `rft_id` is omitted entirely from the second query — the
        point is to stop asking about the article.

        `ignore_date_threshold` is accepted and ignored: Alma does not
        filter on coverage dates (see the module docstring), so emitting
        a second variant would double traffic for identical answers.
        """
        base_params = dict(OPENURL_CONTEXT_PARAMS)
        base_params["svc_dat"] = _SVC_DAT

        doi_params = dict(base_params)
        doi_params["rft_id"] = f"info:doi/{req.doi}"
        doi_params["sid"] = req.sid
        urls = [f"{self.openurl_base}?{urlencode(doi_params)}"]

        if req.issn:
            issn_params = dict(base_params)
            issn_params["rft.issn"] = req.issn
            if req.pub_date:
                issn_params["rft.date"] = req.pub_date
            if req.volume:
                issn_params["rft.volume"] = req.volume
            issn_params["sid"] = req.sid
            urls.append(f"{self.openurl_base}?{urlencode(issn_params)}")
        return urls

    def parse(self, xml_text: str) -> list[FulltextTarget] | None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.debug("Alma uresolver XML parse failed: %s", e)
            return None

        targets: list[FulltextTarget] = []
        for el in root.iter():
            if local_name(el) != "context_service":
                continue
            if el.get("service_type") != FULLTEXT_SERVICE_TYPE:
                continue
            url = ""
            keys: dict[str, str] = {}
            for child in el:
                cn = local_name(child)
                if cn == "resolution_url":
                    url = (child.text or "").strip()
                elif cn == "keys":
                    for k in child:
                        kid = k.get("id") or local_name(k)
                        keys[kid] = (k.text or "").strip()
            if not url:
                continue
            targets.append(FulltextTarget(
                url=url,
                package_name=(
                    keys.get("package_public_name")
                    or keys.get("package_name", "")
                ),
                interface_name=keys.get("interface_name", ""),
                coverage=keys.get("Availability") or keys.get("availability", ""),
                is_free=(keys.get("Is_free", "").strip().lower() in _TRUE_WORDS),
            ))
        return targets
