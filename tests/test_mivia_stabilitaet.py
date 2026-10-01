"""MiViA-fork: Serverstabilitaet -- toter Browser, Logging auf stderr, cp1252."""

import io
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

import linkedin_mcp_server.drivers.browser as browser_module
from linkedin_mcp_server.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _reset():
    browser_module.reset_browser_for_testing()
    yield
    browser_module.reset_browser_for_testing()


def _browser(*, page_closed=False, connected=True, page=True, context=True):
    b = MagicMock()
    b._page = MagicMock() if page else None
    if page:
        b._page.is_closed = MagicMock(return_value=page_closed)
    b._context = MagicMock() if context else None
    if context:
        b._context.browser.is_connected = MagicMock(return_value=connected)
    return b


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_closed": True},
        {"connected": False},
        {"page": False},
        {"context": False},
    ],
)
async def test_toter_browser_wird_beim_naechsten_aufruf_neu_gestartet(
    monkeypatch, kwargs
):
    dead = _browser(**kwargs)
    fresh = _browser()
    browser_module._browser = dead

    async def fake_close():
        browser_module._browser = None

    closer = AsyncMock(side_effect=fake_close)
    monkeypatch.setattr(browser_module, "_close_browser_locked", closer)
    monkeypatch.setattr(
        browser_module, "_create_browser", AsyncMock(return_value=fresh)
    )

    assert await browser_module.get_or_create_browser() is fresh
    closer.assert_awaited_once()


async def test_lebender_browser_wird_wiederverwendet(monkeypatch):
    alive = _browser()
    browser_module._browser = alive
    create = AsyncMock()
    monkeypatch.setattr(browser_module, "_create_browser", create)
    assert await browser_module.get_or_create_browser() is alive
    create.assert_not_awaited()


def test_lebenspruefung_wirft_nie():
    b = _browser()
    b._page.is_closed = MagicMock(side_effect=RuntimeError("boom"))
    assert browser_module._browser_is_dead(b) is False


def test_mock_ohne_klare_antwort_gilt_als_lebendig():
    # MagicMock-Rueckgaben sind weder True noch False -> kein Fehlalarm.
    b = MagicMock()
    assert browser_module._browser_is_dead(b) is False


def test_logging_geht_nie_auf_stdout(monkeypatch):
    fake_out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_out)
    try:
        configure_logging("DEBUG", json_format=True)
        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                assert handler.stream is not fake_out
        logging.getLogger("linkedin_mcp_server.test").warning("stdio rein")
    finally:
        configure_logging("WARNING")
    assert fake_out.getvalue() == ""


def test_emoji_logging_auf_cp1252_stderr_wirft_nicht(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(logging, "raiseExceptions", False)
    try:
        configure_logging("INFO", json_format=False)
        logging.getLogger("linkedin_mcp_server.test").warning("Erfolg ✅ 🔗")
        stream.flush()
    finally:
        configure_logging("WARNING")


def test_fremdes_objekt_gilt_nicht_als_tot():
    assert browser_module._browser_is_dead(object()) is False
