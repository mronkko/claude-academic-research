"""Scopus — abstract retrieval via pybliometrics.

pybliometrics reads its own config file (~/.config/pybliometrics.cfg)
and handles authentication independently of the plugin's config.toml.
"""

from __future__ import annotations

import logging

from fetchers.base import (
    AbstractFetcher,
    AbstractWithheld,
    SourceUnavailable,
    is_not_found,
)

logger = logging.getLogger(__name__)


class ScopusSource(AbstractFetcher):
    name = "scopus"

    def fetch_abstract(
        self, doi: str, *, title=None, cache_dir=None, meta=None,
    ) -> str | None:
        try:
            from pybliometrics.utils.startup import init
            init()
            from pybliometrics.scopus import AbstractRetrieval
        except Exception as e:
            raise SourceUnavailable(f"pybliometrics would not start: {e}") from e

        try:
            a = AbstractRetrieval(doi, id_type="doi", view="FULL")
        except Exception as e:
            # Scopus404Error: Scopus does not index the DOI — an answer.
            # A 429 or 5xx is not, and must reach the log as one.
            if is_not_found(e):
                return None
            if type(e).__name__ != "Scopus401Error":
                raise
            # The FULL view checks entitlement before it looks the record
            # up, so a DOI Scopus has never heard of also answers 401
            # (seen 2026-09-23 for 10.9999/not-a-doi-xyz). META answers
            # 404 for those; one META call tells the two apart.
            try:
                AbstractRetrieval(doi, id_type="doi", view="META")
            except Exception as meta_err:
                if is_not_found(meta_err):
                    return None
                raise
            raise AbstractWithheld(
                "Scopus holds the record but this key may not view its "
                "abstract",
            ) from e

        text = a.abstract
        if not text:
            return None
        # pybliometrics puts the publisher's copyright notice inside
        # `.abstract` and also exposes it alone as `.copyright`; 65 of 82
        # Scopus abstracts in one run carried it. Handing the exact
        # string over removes it wherever it sits, without guessing.
        from abstract_clean import clean_abstract
        return clean_abstract(
            str(text), copyright=str(getattr(a, "copyright", "") or ""),
        )
