"""Fork extension: the outcome after the publish click (incident 2026-10-12).

Every test drives ext_post_outcome with a scripted page: a sequence of
composer states (one per poll) and a sequence of activity-card lists (one per
read-back attempt). Nothing here talks to LinkedIn.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.linkedin import ext_post_outcome as oc
from linkedin_mcp_server.linkedin.ext_post import ExtPostComposer

TEXT = (
    "Härteprüfung „automatisiert“ – 15× schneller 🚀\n\n"
    "Wie wir Korngrößen nach ASTM E112 messen: https://mivia.ai/blog/korn "
    "#Metallografie und was das im Labor bedeutet, mit allen Details."
)
URN = "urn:li:activity:7300000000000000001"


def card(text: str, urn: str = URN, age: str = "Gerade eben") -> dict[str, Any]:
    return {"urn": urn, "text": f"Frederik Stadler\n{age} • \n{text}"}


OPEN = {"editor": True, "editor_chars": 200}
CLOSED = {"editor": False, "editor_chars": 0}


class ScriptPage:
    def __init__(
        self,
        states: list[dict[str, Any]],
        feeds: list[list[dict[str, Any]]] | None = None,
        prompt_click: bool = True,
    ) -> None:
        self.states = list(states)
        self.feeds = list(feeds or [[]])
        self.prompt_click = prompt_click
        self.polls = 0
        self.reads = 0
        self.prompt_clicks = 0

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is oc.STATE_JS:
            self.polls += 1
            return self.states[min(self.polls - 1, len(self.states) - 1)]
        if script is oc.CARDS_JS:
            self.reads += 1
            return self.feeds[min(self.reads - 1, len(self.feeds) - 1)]
        if script is oc.DISCARD_PROMPT_JS:
            assert "posten" in arg["post"]
            self.prompt_clicks += 1
            return {"clicked": self.prompt_click, "count": 1 if self.prompt_click else 0}
        raise AssertionError("unexpected script")


class Env:
    def __init__(self) -> None:
        self.slept = 0.0
        self.urls: list[str] = []
        self.discards = 0

    async def sleep(self, s: float) -> None:
        self.slept += s

    async def navigate(self, url: str) -> None:
        self.urls.append(url)

    async def discard(self, result: dict[str, Any]) -> None:
        self.discards += 1
        result["cleanup"] = "closed+discarded"


async def decide(page: ScriptPage, env: Env) -> dict[str, Any]:
    return await oc.decide(
        page=page,
        editor="[ed]",
        sleep=env.sleep,
        navigate=env.navigate,
        discard=env.discard,
        post_words=["posten", "post"],
        text=TEXT,
        activity_url="https://www.linkedin.com/in/me/recent-activity/all/",
    )


# -- normalisation -------------------------------------------------------------


def test_normalize_drops_url_quotes_emoji_and_hashtag_rendering() -> None:
    shown = (
        'Härteprüfung "automatisiert" - 15x schneller\n'
        "Wie wir Korngrößen nach ASTM E112 messen: hashtag#Metallografie"
    )
    assert oc.normalize(shown).startswith("härteprüfung automatisiert 15x schneller")
    assert "https" not in oc.normalize(TEXT)
    assert "metallografie" in oc.normalize(shown)


def test_link_preview_stripped_url_still_matches() -> None:
    shown = TEXT.replace("https://mivia.ai/blog/korn ", "")
    assert oc.card_matches(card(shown)["text"], TEXT)


def test_truncated_card_matches_on_its_visible_part() -> None:
    shown = "Härteprüfung „automatisiert“ – 15× schneller 🚀\n\nWie wir … mehr"
    assert oc.card_matches(card(shown)["text"], TEXT)


def test_truncated_card_with_other_text_does_not_match() -> None:
    shown = "Härteprüfung „automatisiert“ – 15× schneller 🚀\n\nGanz anders … mehr"
    assert not oc.card_matches(card(shown)["text"], TEXT)


def test_short_shared_opening_does_not_match_untruncated_card() -> None:
    assert not oc.card_matches(card("Härteprüfung heute im Labor")["text"], TEXT)


@pytest.mark.parametrize(
    ("label", "seconds"),
    [("Gerade eben", 0), ("5 Min.", 300), ("2 Std.", 7200), ("3h", 10800),
     ("1 Tag", 86400), ("2d", 172800), ("1 Wo.", 604800)],
)
def test_card_age(label: str, seconds: int) -> None:
    assert oc.card_age_seconds(card("x", age=label)["text"]) == seconds


# -- after the click -----------------------------------------------------------


@pytest.mark.asyncio
async def test_composer_closes_slowly_is_polled_not_checked_once() -> None:
    page = ScriptPage([OPEN] * 8 + [CLOSED], [[card(TEXT)]])
    env = Env()
    r = await decide(page, env)
    assert page.polls == 9
    assert r["outcome"] == "posted" and r["status"] == "posted_verified"
    assert r["post_url"].endswith(f"{URN}/")
    assert env.discards == 0


@pytest.mark.asyncio
async def test_feed_lag_is_retried_with_backoff() -> None:
    # Feed lag = the page renders, but older cards only (an empty page is now
    # a render wait inside one attempt, see test_read_activity_waits_for_cards).
    old = card("Ein aelterer Beitrag ohne Bezug zum neuen Text, lang genug.")
    page = ScriptPage([CLOSED], [[old], [old], [old], [old, card(TEXT)]])
    env = Env()
    r = await decide(page, env)
    assert r["status"] == "posted_verified"
    assert r["readback"]["attempts"] == 4
    assert env.slept >= sum(oc.READBACK_WAITS[:4])


@pytest.mark.asyncio
async def test_closed_but_never_read_back_is_posted_unverified_after_60s() -> None:
    page = ScriptPage([CLOSED], [[]])
    env = Env()
    r = await decide(page, env)
    assert r["status"] == "posted_unverified"
    assert r["outcome"] == "posted" and r["posted"] is True
    assert r["readback"]["attempts"] == len(oc.READBACK_WAITS)
    assert sum(oc.READBACK_WAITS) >= 60


@pytest.mark.asyncio
async def test_success_toast_urn_is_kept_when_feed_lags() -> None:
    page = ScriptPage([{**OPEN, "toast_urn": URN}], [[]])
    r = await decide(page, Env())
    assert r["status"] == "posted_unverified"
    assert r["post_url"].endswith(f"{URN}/")


@pytest.mark.asyncio
async def test_old_identical_post_does_not_verify_a_new_one() -> None:
    page = ScriptPage([CLOSED], [[card("Kommentar", "urn:li:activity:9"), card(TEXT, age="1 Wo.")]])
    r = await decide(page, Env())
    assert r["status"] == "posted_unverified"


@pytest.mark.asyncio
async def test_composer_still_open_with_text_is_not_posted_and_discarded() -> None:
    page = ScriptPage([OPEN], [[]])
    env = Env()
    r = await decide(page, env)
    assert r["outcome"] == "not_posted" and r["status"] == "not_posted"
    assert r["posted"] is False and r["retry_safe"] is True
    assert env.discards == 1
    assert r["settle"]["waited"] >= oc.SETTLE_TIMEOUT


@pytest.mark.asyncio
async def test_open_composer_but_post_went_live_is_posted() -> None:
    page = ScriptPage([OPEN], [[card(TEXT)]])
    env = Env()
    r = await decide(page, env)
    assert r["status"] == "posted_verified"
    assert env.discards == 1  # the stale composer is still closed cleanly


@pytest.mark.asyncio
async def test_error_toast_must_persist_then_not_posted() -> None:
    err = {**OPEN, "alert": "etwas ist schiefgelaufen"}
    page = ScriptPage([err, err, err], [[]])
    env = Env()
    r = await decide(page, env)
    assert page.polls == oc.ERROR_POLLS
    assert r["status"] == "not_posted"
    assert r["error"] == "etwas ist schiefgelaufen"
    assert env.discards == 1


@pytest.mark.asyncio
async def test_transient_announcement_while_posting_is_not_an_error() -> None:
    blip = {**OPEN, "alert": "wird gepostet"}
    page = ScriptPage([blip, OPEN, CLOSED], [[card(TEXT)]])
    env = Env()
    r = await decide(page, env)
    assert r["status"] == "posted_verified"
    assert env.discards == 0


@pytest.mark.asyncio
async def test_save_as_draft_prompt_is_discarded() -> None:
    page = ScriptPage([OPEN, {**OPEN, "draft_prompt": True}], [[]])
    env = Env()
    r = await decide(page, env)
    assert r["outcome"] == "draft_saved" and r["status"] == "draft_discarded"
    assert r["posted"] is False and r["prompt_discarded"] is True
    assert page.prompt_clicks == 1
    assert env.discards == 0


@pytest.mark.asyncio
async def test_discard_post_modal_is_confirmed_as_not_posted() -> None:
    page = ScriptPage([{**CLOSED, "discard_prompt": True}], [[]])
    r = await decide(page, Env())
    assert r["status"] == "not_posted" and r["prompt_discarded"] is True


@pytest.mark.asyncio
async def test_prompt_without_single_discard_button_falls_back_to_cleanup() -> None:
    page = ScriptPage([{**OPEN, "draft_prompt": True}], [[]], prompt_click=False)
    env = Env()
    r = await decide(page, env)
    assert env.discards == 1
    assert r["retry_safe"] is False


@pytest.mark.asyncio
async def test_open_composer_without_text_stays_unknown() -> None:
    page = ScriptPage([{"editor": True, "editor_chars": 0}], [[]])
    r = await decide(page, Env())
    assert r["outcome"] == "unknown" and r["status"] == "post_unconfirmed"


@pytest.mark.asyncio
async def test_upload_pending_waits_then_gives_up() -> None:
    env = Env()
    busy = ScriptPage([{**OPEN, "busy": True}])
    assert not await oc.wait_for_upload(busy, "[ed]", env.sleep, timeout=5)
    done = ScriptPage([{**OPEN, "busy": True}, {**OPEN, "busy": True}, OPEN])
    assert await oc.wait_for_upload(done, "[ed]", env.sleep, timeout=5)


def test_duplicate_within_24h_and_unknown_age_refuse() -> None:
    assert oc.recent_duplicate(oc.find_matches([card(TEXT, age="5 Std.")], TEXT))
    assert oc.recent_duplicate(oc.find_matches([card(TEXT, age="")], TEXT))
    assert not oc.recent_duplicate(oc.find_matches([card(TEXT, age="2 Tage")], TEXT))


# -- composer level: duplicate refusal and verify_post --------------------------


class ComposerPage:
    def __init__(self, feed: list[dict[str, Any]]) -> None:
        self.feed = feed
        self.clicked: list[str] = []

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if script is oc.CARDS_JS:
            return self.feed
        raise AssertionError("composer must not be touched")

    async def wait_for_selector(self, *_: Any, **__: Any) -> None:
        raise AssertionError("composer must not open")

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)


class Session:
    def __init__(self, page: Any) -> None:
        self.page = page

    async def delay(self, _: float) -> None:
        return None

    async def check_rate_limit(self) -> None:
        return None


class Nav:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def _navigate_to_page(self, url: str) -> None:
        self.urls.append(url)


@pytest.mark.asyncio
async def test_identical_live_post_refuses_publish_before_composer() -> None:
    page = ComposerPage([card(TEXT, age="3 Std.")])
    nav = Nav()
    r = await ExtPostComposer(Session(page), nav).create_post(  # type: ignore[arg-type]
        TEXT, image_path=None, confirm_post=True
    )
    assert r["status"] == "duplicate_live" and r["posted"] is False
    assert r["post_url"].endswith(f"{URN}/")
    assert nav.urls == ["https://www.linkedin.com/in/me/recent-activity/all/"]
    assert not page.clicked


@pytest.mark.asyncio
async def test_verify_post_finds_and_reports_url() -> None:
    page = ComposerPage([card("anders", "urn:li:activity:1"), card(TEXT, age="2 Tage")])
    r = await ExtPostComposer(Session(page), Nav()).verify_post(TEXT)  # type: ignore[arg-type]
    assert r["status"] == "found"
    assert r["activity_id"] == URN.rsplit(":", 1)[1]
    assert r["age_seconds"] == 172800


@pytest.mark.asyncio
async def test_verify_post_not_found_is_read_only() -> None:
    page = ComposerPage([card("anders")])
    r = await ExtPostComposer(Session(page), Nav()).verify_post(TEXT, wait=True)  # type: ignore[arg-type]
    assert r["status"] == "not_found" and r["post_url"] is None
    assert r["attempts"] == len(oc.READBACK_WAITS)
    assert not page.clicked


# Live 2026-10-12: card text starts with header chrome, the post follows the age.
def test_card_body_skips_header_chrome():
    card = ("Feed-Beitrag\nBoosten Sie diesen Beitrag\nJessica Schneider\n• Sie\n"
            "Wir helfen Herstellern\nIhre Serviceleistungen anzeigen\n2 Std.\n"
            "Morgen geht es los: Ab Dienstag bin ich auf dem HärtereiKongress in Köln.")
    assert oc.card_body(card).startswith("Morgen geht es los")


def test_card_body_without_age_keeps_text():
    assert oc.card_body("nur Text ohne Alter") == "nur Text ohne Alter"


def test_card_body_just_now():
    assert oc.card_body("Kopf\nGerade eben\nHallo Welt") == "Hallo Welt"


def test_cards_js_scans_all_attributes_and_encoded_urns():
    js = oc.CARDS_JS
    assert "document.querySelectorAll('*')" in js
    assert "%3A" in js
    assert "acts(p.outerHTML).size > 1" in js



async def test_read_activity_waits_for_cards_to_render():
    # Live 2026-10-12: the first evaluate after navigation returned no card.
    answers = iter([[], [], [{"urn": "urn:li:activity:7515394313292390400", "text": "2 Std.\nHallo Welt Beitrag"}]])

    class Page:
        async def evaluate(self, js, arg=None):
            return next(answers)

    slept = []

    async def sleep(s):
        slept.append(s)

    async def navigate(url):
        return None

    rb = await oc.read_activity(Page(), navigate, sleep, "Hallo Welt Beitrag", "u", waits=(0.0,), want_fresh=False)
    assert rb["found"] and rb["urn"].endswith("7515394313292390400")
    assert slept.count(oc.CARD_RENDER_INTERVAL) == 2
