"""ext_daily reads the attendee count like the tool: it waits for the late line."""

import asyncio

from linkedin_mcp_server import ext_daily


def test_daily_event_waits_for_the_late_attendee_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_LEDGER", str(tmp_path / "ledger.jsonl"))
    calls = {"n": 0}

    class Page:
        url = "https://www.linkedin.com/events/1/"

        async def evaluate(self, *_a):
            calls["n"] += 1
            return None if calls["n"] < 4 else 42

    class Session:
        page = Page()
        t = 0.0

        def monotonic(self):
            return self.t

        async def check_rate_limit(self):
            return None

        async def delay(self, s):
            self.t += s

    class Nav:
        async def _navigate_to_page(self, _url):
            return None

    class Ex:
        ext_session = Session()
        ext_navigator = Nav()

    c = ext_daily.Collector(Ex(), {}, tmp_path)

    async def goto(_url):
        return None

    monkeypatch.setattr(c, "_goto", goto)
    out = asyncio.run(c.event("7457346711301214208", {"search_reserve": 10000}))
    assert out["attendee_count"] == 42
    assert calls["n"] == 4


def test_daily_event_gives_up_after_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_LEDGER", str(tmp_path / "ledger.jsonl"))
    from linkedin_mcp_server.linkedin import ext_network

    class Page:
        async def evaluate(self, *_a):
            return None

    class Session:
        t = 0.0

        def monotonic(self):
            return self.t

        async def delay(self, s):
            self.t += s

    s = Session()
    assert asyncio.run(ext_network.read_event_count(Page(), s)) is None
    assert s.t >= ext_network.EVENT_COUNT_WAIT_SECONDS
