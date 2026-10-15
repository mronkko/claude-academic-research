"""`core.publisher_consent`: the stored answers and how a run reads them.

The rule under test throughout: **a missing answer never means yes**.
Only a recorded `allow` lets automated download proceed without a
question; everything else is a question when someone can be asked and a
skip (`NEEDED` → `CONSENT_NEEDED`) when nobody can.

The suite-wide fixture points HOME and `config_loader.CONFIG_PATH` at a
temporary directory, so every read and write here hits a scratch file.
"""

from __future__ import annotations

import datetime as dt
import tomllib

import pytest

from core import config_loader, config_writer, publisher_consent as pc


def _cfg() -> dict:
    return tomllib.loads(config_loader.CONFIG_PATH.read_text(encoding="utf-8"))


def _write(text: str) -> None:
    config_loader.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_loader.CONFIG_PATH.write_text(text, encoding="utf-8")
    config_loader.load_config.cache_clear()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("automated", "can_ask", "verdict"), [
    ("allow", False, pc.PROCEED),
    ("allow", True, pc.PROCEED),
    ("deny", False, pc.DENIED),
    ("deny", True, pc.DENIED),
    ("ask", True, pc.ASK_USER),
    ("ask", False, pc.NEEDED),
])
def test_resolve_by_stored_answer(automated, can_ask, verdict) -> None:
    consent = pc.Consent(site="sage", automated=automated, stored=True)
    assert pc.resolve(consent, can_ask=can_ask) == verdict


def test_a_missing_entry_is_ask_and_never_proceeds() -> None:
    consent = pc.lookup("sage")
    assert consent.automated == pc.ASK and not consent.stored
    assert pc.resolve(consent, can_ask=False) == pc.NEEDED
    assert pc.resolve(consent, can_ask=True) == pc.ASK_USER


def test_an_unreadable_answer_reads_as_ask_not_allow() -> None:
    _write('[publisher_consent.sage]\nautomated = "alow"\n')
    consent = pc.lookup("sage")
    assert consent.automated == pc.ASK
    assert consent.invalid == "alow"
    assert pc.resolve(consent, can_ask=False) == pc.NEEDED


def test_a_non_table_entry_reads_as_ask() -> None:
    _write('[publisher_consent]\nsage = "allow"\n')
    consent = pc.lookup("sage")
    assert consent.automated == pc.ASK and consent.invalid


def test_read_all_keeps_dates_and_quoted_host_keys() -> None:
    _write(
        '[publisher_consent.sage]\nautomated = "allow"\nconfirmed = 2026-10-15\n\n'
        '[publisher_consent."degruyter.com"]\nautomated = "deny"\n'
    )
    got = pc.read_all()
    assert got["sage"].confirmed == dt.date(2026, 10, 15)
    assert got["sage"].automated == pc.ALLOW
    assert got["degruyter.com"].automated == pc.DENY
    assert got["degruyter.com"].confirmed is None


@pytest.mark.parametrize("bad", ["", "Sage Journals", "-sage", "a/b", "x" * 80])
def test_bad_site_keys_are_refused(bad) -> None:
    with pytest.raises(pc.ConsentError):
        pc.normalise_site(bad)


def test_site_keys_are_lower_cased() -> None:
    assert pc.normalise_site(" JSTOR ") == "jstor"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_record_writes_the_answer_with_the_date() -> None:
    got = pc.record("sage", "allow", today=dt.date(2026, 10, 15))
    assert got.automated == pc.ALLOW and got.stored
    table = _cfg()["publisher_consent"]["sage"]
    assert table == {"automated": "allow", "confirmed": dt.date(2026, 10, 15)}
    assert pc.lookup("sage").confirmed == dt.date(2026, 10, 15)


def test_record_keeps_everything_else_in_the_file() -> None:
    _write(
        '[zotero]\napi_key = "k"\n\n[library]\nno_access = ["aom"]\n\n'
        '[publisher_consent.tandf]\nautomated = "deny"\nconfirmed = 2026-01-02\n'
    )
    pc.record("jstor", "allow", today=dt.date(2026, 10, 15))
    cfg = _cfg()
    assert cfg["zotero"]["api_key"] == "k"
    assert cfg["library"]["no_access"] == ["aom"]
    assert cfg["publisher_consent"]["tandf"]["automated"] == "deny"
    assert cfg["publisher_consent"]["jstor"]["automated"] == "allow"


def test_record_replaces_an_earlier_answer() -> None:
    pc.record("sage", "allow", today=dt.date(2026, 1, 1))
    pc.record("sage", "deny", today=dt.date(2026, 10, 15))
    assert pc.lookup("sage").automated == pc.DENY
    assert pc.lookup("sage").confirmed == dt.date(2026, 10, 15)


def test_record_quotes_host_keys() -> None:
    pc.record("degruyter.com", "allow", today=dt.date(2026, 10, 15))
    assert '[publisher_consent."degruyter.com"]' in config_loader.CONFIG_PATH.read_text()
    assert pc.lookup("degruyter.com").automated == pc.ALLOW


def test_record_refuses_an_unknown_answer() -> None:
    with pytest.raises(pc.ConsentError):
        pc.record("sage", "yes")


def test_set_table_refuses_to_replace_a_scalar() -> None:
    _write('publisher_consent = "x"\n')
    with pytest.raises(ValueError):
        config_writer.set_table(("publisher_consent", "sage"), {"automated": "allow"})
