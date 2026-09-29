"""MiViA fork: daily collector -- pure parts and exit codes (no browser)."""

from __future__ import annotations

import asyncio
import json

import pytest

from linkedin_mcp_server import mivia_daily


def test_hashtag_filter_is_case_insensitive_and_needs_the_hash():
    text = "One of several highlights at the HeatTreatmentCongress ... #ifhtse #HK2026"
    assert mivia_daily.has_hashtag(text, ["hk2026"])
    assert mivia_daily.has_hashtag(text, ["#ifhtse"])
    # Without '#' the plain word does not count: "HeatTreatmentCongress" appears
    # in the text but not as a hashtag.
    assert not mivia_daily.has_hashtag(text, ["heattreatmentcongress"])
    assert not mivia_daily.has_hashtag("", ["hk2026"])


def test_company_posts_view_as_member_and_admin_redirect_refused(tmp_path, monkeypatch):
    assert mivia_daily.company_posts_url("mivia").endswith(
        "/company/mivia/posts/?viewAsMember=true"
    )
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))

    class Page:
        url = "https://www.linkedin.com/company/81728804/admin/dashboard/"

        async def evaluate(self, *_a):
            return [{"urn": "urn:li:activity:1", "text": "fremd"}]

    class Session:
        page = Page()

        async def check_rate_limit(self):
            return None

        async def delay(self, _s):
            return None

    class Nav:
        async def _navigate_to_page(self, _url):
            return None

    class Ex:
        _mivia_session = Session()
        _mivia_navigator = Nav()

    c = mivia_daily.Collector(Ex(), {}, tmp_path)
    with pytest.raises(RuntimeError, match="admin view"):
        asyncio.run(c._urns(mivia_daily.company_posts_url("mivia"), 3))


def test_post_head_skips_card_labels():
    text = "Nummer des Feedbeitrags 1\nFeed-Beitrag\n4 Monat(e)\nVor 4 Jahren waren wir das erste Mal auf der CONTROL."
    assert (
        mivia_daily.post_head(text)
        == "Vor 4 Jahren waren wir das erste Mal auf der CONTROL."
    )


def test_activity_id():
    assert (
        mivia_daily.activity_id("urn:li:activity:7510583477378134016")
        == "7510583477378134016"
    )


class _BusyError(Exception):
    pass


_BusyError.__name__ = "BrowserBusyError"


@pytest.mark.parametrize(
    "exc_name,status,code",
    [
        ("BrowserBusyError", "browser_busy", 4),
        ("AuthenticationError", "login_required", 5),
        ("RuntimeError", "failed", 1),
    ],
)
def test_exit_codes_and_report_on_startup_failure(
    tmp_path, monkeypatch, exc_name, status, code
):
    exc_type = type(exc_name, (Exception,), {})

    async def fail(*_a, **_k):
        raise exc_type("nope")

    import linkedin_mcp_server.drivers.browser as browser

    monkeypatch.setattr(browser, "get_or_create_browser", fail)
    monkeypatch.setattr(browser, "set_headless", lambda _h: None)
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    rc = mivia_daily.main(
        ["--config", str(cfg), "--out", str(out), "--state-dir", str(tmp_path)]
    )
    assert rc == code
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == status and report["schema"] == "mivia_daily.v1"


def test_a_failing_part_does_not_stop_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("MIVIA_LINKEDIN_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("MIVIA_LINKEDIN_ENGAGERS_SEEN", str(tmp_path / "seen.json"))

    class Ex:
        _mivia_session = object()
        _mivia_navigator = object()

    c = mivia_daily.Collector(Ex(), {"radar": {"enabled": False}}, tmp_path)

    async def boom():
        raise ValueError("kaputt")

    async def ok():
        return {"enabled": True, "total_viewers": 1, "new_viewers": []}

    c.own_posts = boom  # type: ignore[method-assign]
    c.viewers = ok  # type: ignore[method-assign]
    report = asyncio.run(c.run())
    assert report["posts"] == []
    assert report["viewers"]["total_viewers"] == 1
    assert report["errors"][0]["part"] == "own_posts"
