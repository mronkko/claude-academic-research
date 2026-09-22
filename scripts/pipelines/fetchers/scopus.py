"""Scopus — abstract retrieval via pybliometrics.

pybliometrics reads its own config file (~/.config/pybliometrics.cfg)
and handles authentication independently of the plugin's config.toml.
"""

from __future__ import annotations

import logging

from fetchers.base import AbstractFetcher

logger = logging.getLogger(__name__)


class ScopusSource(AbstractFetcher):
    name = "scopus"

    def fetch_abstract(self, doi: str, *, title=None, cache_dir=None) -> str | None:
        try:
            from pybliometrics.utils.startup import init
            init()
            from pybliometrics.scopus import AbstractRetrieval
        except Exception as e:
            logger.debug("pybliometrics import/init failed: %s", e)
            return None

        try:
            a = AbstractRetrieval(doi, id_type="doi", view="FULL")
        except Exception as e:
            logger.debug("Scopus AbstractRetrieval(%s) failed: %s", doi, e)
            return None

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
