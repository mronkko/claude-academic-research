"""Ex Libris SFX / plain OpenURL.

Response shape: a `<target>` element per route, holding a
`<service_type>` child and a `<target_url>`. SFX wraps those in
`<targets>` or `<target_set>` depending on version, so the parse walks
every element and pairs service type with URL *within* one target rather
than assuming a fixed depth.

`sfx.response_type=multi_obj_xml` is what makes SFX emit this XML at
all, and `sfx.ignore_date_threshold=1` is what lets a caller ask the
coverage question twice — once date-filtered, once not — to tell "the
library has no relationship with this publisher" from "it has the
publisher but not this year". Alma has no equivalent, which is why
`supports_date_threshold` exists on the base class.

Provider names
--------------
SFX targets normally carry `<target_public_name>` (and sometimes
`<target_name>`), which the shared ranking can use. Extraction is
best-effort: this repo has no committed SFX fixture to verify against
(`tests/fixtures/sfx/` is gitignored, being institution-specific), so a
response without those elements must still rank correctly. It does —
SFX emits real publisher and EZproxy URLs, so host matching alone is
sufficient here, and names are a bonus rather than the mechanism.
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


class SfxResolver(LibraryResolver):
    flavour = "sfx"
    supports_date_threshold = True

    @classmethod
    def matches(cls, openurl_base: str) -> bool:
        """Accepts any non-empty endpoint.

        SFX is the fallback dialect: plain OpenURL has no distinguishing
        path segment the way Alma's `/view/uresolver/` does, so "not
        recognisably something else" is the only available test. The
        registry in `__init__.py` therefore offers every more specific
        flavour first — order there is load-bearing, and this method
        must stay last-resort.
        """
        return bool(openurl_base)

    def query_urls(self, req: ResolverRequest) -> list[str]:
        params = dict(OPENURL_CONTEXT_PARAMS)
        params["sfx.response_type"] = "multi_obj_xml"
        params["rft_id"] = f"info:doi/{req.doi}"
        params["sfx.sid"] = req.sid
        if req.ignore_date_threshold:
            params["sfx.ignore_date_threshold"] = "1"
        return [f"{self.openurl_base}?{urlencode(params)}"]

    def parse(self, xml_text: str) -> list[FulltextTarget] | None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.debug("SFX XML parse failed: %s", e)
            return None

        targets: list[FulltextTarget] = []
        for el in root.iter():
            if local_name(el) != "target":
                continue
            is_fulltext = False
            url = ""
            public_name = ""
            name = ""
            for child in el:
                cn = local_name(child)
                text = (child.text or "").strip()
                if cn == "service_type" and text == FULLTEXT_SERVICE_TYPE:
                    is_fulltext = True
                elif cn == "target_url":
                    url = text
                elif cn == "target_public_name":
                    public_name = text
                elif cn == "target_name":
                    name = text
            if is_fulltext and url:
                targets.append(FulltextTarget(
                    url=url,
                    package_name=public_name or name,
                    interface_name=name,
                ))
        return targets
