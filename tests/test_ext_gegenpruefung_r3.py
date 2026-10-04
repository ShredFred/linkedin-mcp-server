"""Gegenpruefung Haertungsrunde 3 (c22c76d, 22c7a16, f9995b7, 5d70a54)."""

from types import SimpleNamespace

from linkedin_mcp_server.drivers.browser import _browser_is_dead
from linkedin_mcp_server.linkedin.identifiers import landed_identity_mismatch
from linkedin_mcp_server.ext_message_checks import check_outgoing


def _manager(*, page_closed=False, ctx_browser=None):
    page = SimpleNamespace(is_closed=lambda: page_closed)
    context = SimpleNamespace(browser=ctx_browser)
    return SimpleNamespace(_context=context, _page=page)


def test_persistent_context_without_browser_is_alive():
    # launch_persistent_context: context.browser is None -> must not read as dead
    assert _browser_is_dead(_manager(ctx_browser=None)) is False


def test_closed_page_and_disconnected_browser_are_dead():
    assert _browser_is_dead(_manager(page_closed=True)) is True
    gone = SimpleNamespace(is_connected=lambda: False)
    assert _browser_is_dead(_manager(ctx_browser=gone)) is True


def test_business_text_passes_outgoing_check():
    for text in (
        "Termin am 01.10.2026 um 10:30, Preis 3.5 Mio., z.B. ca. 12,5 %",
        "Siehe https://example.com/produkt und www.example.com oder example.com/blog",
        "Profil linkedin.com/in/max-mustermann/ und https://www.linkedin.com/company/ext/",
        "Tel. +49 (0)721 123-456, Mail info@example.com",
        "Buchen: https://calendly.com/acme_jane-doe/30min",
        "Hallo \U0001f468‍\U0001f469‍\U0001f467 ❤️",
    ):
        assert check_outgoing(text, max_utf16=8000) is None, text


def test_opaque_ids_resolving_to_vanity_are_no_redirect():
    url = "https://www.linkedin.com/company/ext-gmbh/about/"
    assert landed_identity_mismatch(url, "company", "123456") is None
    url = "https://www.linkedin.com/in/max-mustermann/"
    assert landed_identity_mismatch(url, "in", "ACoAAB1234xyz") is None


def test_real_slug_redirect_still_reported():
    url = "https://www.linkedin.com/in/someone-else/"
    hit = landed_identity_mismatch(url, "in", "max-mustermann")
    assert hit and hit["landed"] == "someone-else"
