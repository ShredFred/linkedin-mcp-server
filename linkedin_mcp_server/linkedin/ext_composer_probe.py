"""Fork extension: read-only probe of a LinkedIn page's interactive controls.

Why this exists. ``create_company_post`` guesses nothing -- it demands exactly
one matching control and stops otherwise. That is right, but it made the first
productive attempt useless as a *measurement*: the composer at
``/feed/?shareActive=true`` turned out to carry no author control and no
schedule clock at all, and finding the entry point that does would have meant a
code change, a release and a client restart per attempt.

So the measurement gets its own tool. It navigates, optionally opens something
by an explicit label the caller names, and reports what is actually on screen.
It never publishes:

* only ``linkedin.com`` is navigated to;
* a click is performed only for a label the caller passes **and** that does not
  look like a publish, send, schedule-confirm or delete control -- the refusal
  list is checked against the caller's label *and* against the element's own
  text, so a renamed button cannot slip through;
* clicks happen only in the order the caller names, at most four per call,
  and nothing is typed anywhere. A sequence is needed because a dialog can
  only be reached through the control that opens it.

The report is deliberately verbose: tag, role, aria-label, text, enabled state
and whether the element sits inside a dialog. That is what distinguishes "the
control is missing" from "the control is there under a name we did not try".
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

# Never clicked, whatever the caller asks for. Substring match, lowercase.
FORBIDDEN_CLICK = (
    "posten",
    "post",
    "veröffentlichen",
    "veroeffentlichen",
    "publish",
    "senden",
    "send",
    "planen",
    "schedule",
    "löschen",
    "loeschen",
    "delete",
    "entfernen",
    "remove",
    "bewerben",
    "apply",
    "folgen",
    "follow",
    "vernetzen",
    "connect",
)


def check_url(url: str) -> dict[str, Any] | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return {"status": "invalid_input", "field": "url"}
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return {
            "status": "invalid_input",
            "field": "url",
            "message": "Only linkedin.com is probed.",
        }
    return None


def check_click_label(label: str | None) -> dict[str, Any] | None:
    if label is None:
        return None
    low = label.strip().lower()
    if not low:
        return {"status": "invalid_input", "field": "click_label"}
    hit = next((w for w in FORBIDDEN_CLICK if w in low), None)
    if hit:
        return {
            "status": "refused_click",
            "message": f"The label contains {hit!r}; this probe never clicks a "
            "control that could publish, send, schedule or delete.",
        }
    return None


# Report every visible interactive element. ``dialog`` tells a composer dialog
# apart from the page behind it, which is where the earlier guess went wrong.
_REPORT_JS = r"""(limit) => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const sel = 'button, [role="button"], [role="radio"], [role="option"], [role="menuitem"],'
            + ' [role="menuitemradio"], [role="combobox"], [contenteditable="true"], input, select';
  const text = el => (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 90);
  return [...document.querySelectorAll(sel)].filter(visible).slice(0, limit).map(el => ({
    tag: el.tagName.toLowerCase(),
    role: el.getAttribute('role') || '',
    label: (el.getAttribute('aria-label') || '').slice(0, 90),
    text: text(el),
    type: (el.getAttribute('type') || '').slice(0, 20),
    componentkey: (el.getAttribute('componentkey') || '').slice(0, 60),
    disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
    dialog: !!el.closest('[role="dialog"], dialog'),
  }));
}"""

# Mark one element by exact aria-label or exact text. Exactly one or nothing.
_MARK_BY_LABEL_JS = r"""(arg) => {
  document.querySelectorAll('[data-ext-probe]').forEach(e => e.removeAttribute('data-ext-probe'));
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(arg.label);
  const sel = 'button, [role="button"], [role="menuitem"], [role="combobox"], a';
  const hits = [...document.querySelectorAll(sel)].filter(visible).filter(el =>
    norm(el.getAttribute('aria-label')) === want || norm(el.innerText) === want);
  if (hits.length !== 1) return {count: hits.length};
  const el = hits[0];
  // Second barrier: the element's own words, not only the caller's label.
  const own = norm(el.getAttribute('aria-label')) + ' ' + norm(el.innerText);
  const bad = arg.forbidden.find(w => own.includes(w));
  if (bad) return {count: 1, refused: bad};
  el.setAttribute('data-ext-probe', '1');
  return {count: 1};
}"""


class ExtComposerProbe:
    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    @property
    def _page(self) -> Any:
        return self._session.page

    async def probe(
        self,
        url: str,
        *,
        click_labels: list[str],
        limit: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"url": url, "clicked": []}
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.delay(3.0)

        for label in click_labels:
            marked = await self._page.evaluate(
                _MARK_BY_LABEL_JS,
                {"label": label, "forbidden": list(FORBIDDEN_CLICK)},
            )
            if marked.get("refused"):
                result["status"] = "refused_click"
                result["stopped_at"] = label
                result["message"] = (
                    f"The element's own wording contains {marked['refused']!r}; "
                    "it was not clicked."
                )
                result["elements"] = await self._page.evaluate(_REPORT_JS, limit)
                return result
            if marked.get("count") != 1:
                result["status"] = "click_target_not_unique"
                result["stopped_at"] = label
                result["matches"] = marked.get("count")
                result["elements"] = await self._page.evaluate(_REPORT_JS, limit)
                return result
            await self._page.click("[data-ext-probe]")
            await self._session.delay(3.0)
            result["clicked"].append(label)

        result["status"] = "probed"
        result["current_url"] = self._page.url
        result["elements"] = await self._page.evaluate(_REPORT_JS, limit)
        return result
