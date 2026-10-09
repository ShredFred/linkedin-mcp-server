"""Fork extension: images with alt text and person tags, attached like by hand.

Measured 2026-10-09 (German interface) with composer_probe steps on both
composers; fixture ``tests/fixtures/media-editor-de-2026-10-09.json``:

* The file chooser of the media control ("Medieninhalte" on the member
  composer, "Mediendatei hinzufügen" on the page composer) takes several files
  at once and opens an image editor dialog. Its thumbnails keep the upload
  order: on the member composer they read ``alt="image 0"``, ``"image 1"``; on
  the page composer the alt is the file name and each has an
  "Auswählen <file>" button.
* "Alternativer Text" opens a textarea (placeholder "Wie würden Sie dieses Bild
  beschreiben?", 1.000 characters) with "Hinzufügen", which reads
  "Aktualisieren" on a second visit and shows the saved text -- that second
  visit is the read-back.
* "Tag" opens a name search. On the member composer every suggestion carries
  its target id (the same React prop as the mention typeahead); after
  "Hinzufügen" the button reads "Tag, 1 Person getaggt". On the page composer
  the suggestions carry **no** identifier, so tags there are refused
  (``media_tag_unverifiable``) -- a tag by name alone could tag a namesake.
* "Weiter" returns to the composer, which then shows one preview per image and
  an enabled "Posten". No progress indicator appeared for small images; the
  previews and the enabled button are the completion signal.
* Video and documents: the member composer's content types offer only "Event"
  and "Stellenanzeige"; documents are not offered there, and video was not
  measured. Both are refused (``media_kind_unmeasured``).

Every step demands exactly one visible match and stops with a reason code; the
caller discards the composer, which was measured to leave no draft behind.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from linkedin_mcp_server.linkedin.ext_composer_labels import words
from linkedin_mcp_server.linkedin.ext_mentions import (
    Mention,
    _OPTIONS_JS,
    choose_option,
    fold,
    parse_target,
    resolve_targets,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
UNMEASURED_SUFFIXES = {".mp4", ".mov", ".avi", ".webm", ".pdf", ".ppt", ".pptx", ".doc", ".docx"}
MAX_IMAGES = 20
ALT_MAX = 1000


def check_media(items: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Normalise ``media``; (items, refusal). Pure, no browser."""
    if not items:
        return [], None
    if not isinstance(items, list) or len(items) > MAX_IMAGES:
        return [], {"status": "media_too_many", "max": MAX_IMAGES}
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(items):
        if not isinstance(raw, dict) or not raw.get("path"):
            return [], {"status": "media_invalid", "index": i}
        path = Path(str(raw["path"])).expanduser()
        suffix = path.suffix.lower()
        if suffix in UNMEASURED_SUFFIXES:
            return [], {
                "status": "media_kind_unmeasured",
                "index": i,
                "message": "Only images are built; video and documents were not measured.",
            }
        if suffix not in IMAGE_SUFFIXES or not path.is_file():
            return [], {"status": "media_invalid_path", "index": i, "path": str(path)}
        alt = raw.get("alt_text")
        if alt is not None:
            alt = str(alt).strip()
            if len(alt) > ALT_MAX or "\n" in alt:
                return [], {"status": "alt_text_invalid", "index": i, "max": ALT_MAX}
        tags: list[Mention] = []
        for tag in raw.get("tags") or []:
            name = str((tag or {}).get("name") or "").strip() if isinstance(tag, dict) else ""
            target = str((tag or {}).get("target") or "").strip() if isinstance(tag, dict) else ""
            if not name or not target:
                return [], {
                    "status": "media_tag_invalid",
                    "index": i,
                    "message": 'A tag is {"name": ..., "target": slug | URL | URN}.',
                }
            try:
                tags.append(parse_target(name, target))
            except ValueError as exc:
                return [], {"status": "media_tag_invalid", "index": i, "detail": str(exc)}
        out.append({"path": path, "alt_text": alt or None, "tags": tags})
    if len({str(m["path"]) for m in out}) != len(out):
        return [], {"status": "media_invalid", "message": "the same file twice"}
    return out, None


def describe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "file": m["path"].name,
            "alt_text": m["alt_text"],
            "tags": [t.describe() for t in m["tags"]],
        }
        for m in items
    ]


# The image editor: the visible dialog that carries the alt-text control or
# the thumbnails. Reports what read-back needs.
_EDITOR_JS = r"""(arg) => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const own = b => [norm(b.getAttribute('aria-label')), norm(b.innerText)];
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(vis);
  // The image editor and its sub-views (alt text, tags) all carry one of
  // these controls; the composer and the messaging overlay carry none.
  const isEditor = d => [...d.querySelectorAll('button')].filter(vis)
      .some(b => own(b).some(t => arg.anchors.includes(t)));
  const ed = dialogs.filter(isEditor).pop();
  if (!ed) return {editor: false, dialogs: dialogs.length};
  const thumbs = [...ed.querySelectorAll('img')].filter(vis)
      .filter(i => /^(blob|data):/.test(i.src || '') && i.width < 300 && !/vorschau|preview/i.test(i.alt || ''));
  const preview = [...ed.querySelectorAll('img')].filter(vis).find(i => /vorschau|preview/i.test(i.alt || ''));
  const tag = [...ed.querySelectorAll('button')].filter(vis)
      .map(b => own(b).find(t => arg.tag.some(w => t === w || t.startsWith(w + ','))))
      .find(Boolean);
  const area = [...ed.querySelectorAll('textarea')].filter(vis);
  return {editor: true, thumbs: thumbs.map(i => i.alt || ''),
          preview: preview ? (preview.alt || '') : null,
          tag_label: tag || null,
          textarea: area.length === 1 ? area[0].value : (area.length ? '#many' : null)};
}"""

# Mark exactly one visible control in the image editor: by label/text, or the
# nth thumbnail (its button when it sits in one).
_MARK_JS = r"""(arg) => {
  document.querySelectorAll('[data-ext-media]').forEach(e => e.removeAttribute('data-ext-media'));
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const own = b => [norm(b.getAttribute('aria-label')), norm(b.innerText)];
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(vis);
  const ed = dialogs.filter(d => [...d.querySelectorAll('button')].filter(vis)
      .some(b => own(b).some(t => arg.anchors.includes(t)))).pop();
  const composer = arg.composer ? dialogs.find(d => d.querySelector(arg.composer)) : null;
  const root = arg.scope === 'document' ? document : arg.scope === 'composer' ? composer : ed;
  if (!root) return {count: 0, no_editor: true};
  let hits;
  if (arg.thumb !== undefined && arg.thumb !== null) {
    const thumbs = [...root.querySelectorAll('img')].filter(vis)
        .filter(i => /^(blob|data):/.test(i.src || '') && i.width < 300 && !/vorschau|preview/i.test(i.alt || ''));
    const t = thumbs[arg.thumb];
    hits = t ? [t.closest('button, [role="button"]') || t] : [];
  } else {
    hits = [...root.querySelectorAll('button, [role="button"]')].filter(vis).filter(b =>
      own(b).some(t => arg.words.includes(t) || (arg.prefix && arg.words.some(w => t.startsWith(w + ',')))));
  }
  if (hits.length !== 1) return {count: hits.length};
  hits[0].setAttribute('data-ext-media', arg.tag);
  return {count: 1, disabled: hits[0].disabled === true || hits[0].getAttribute('aria-disabled') === 'true'};
}"""

_TEXTAREA_JS = r"""() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const area = [...document.querySelectorAll('[role="dialog"] textarea, dialog textarea')].filter(vis);
  if (area.length !== 1) return {count: area.length};
  area[0].setAttribute('data-ext-media', 'alt-area');
  return {count: 1, value: area[0].value};
}"""

_TAG_INPUT_JS = r"""(arg) => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const hits = [...document.querySelectorAll('[role="dialog"] input, dialog input')].filter(vis)
      .filter(i => [i.getAttribute('placeholder'), i.getAttribute('aria-label')]
          .some(v => arg.words.includes(norm(v))));
  if (hits.length !== 1) return {count: hits.length};
  hits[0].setAttribute('data-ext-media', 'tag-input');
  hits[0].focus();
  return {count: 1};
}"""

# The composer after "Weiter": previews of the attached images.
_COMPOSER_PREVIEWS_JS = r"""(arg) => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const dialogs = [...document.querySelectorAll('[role="dialog"], dialog')].filter(vis);
  const composer = dialogs.find(d => d.querySelector(arg.editor));
  if (!composer) return null;
  return [...composer.querySelectorAll('img')].filter(vis)
      .filter(i => /^(blob|data):/.test(i.src || '')).length;
}"""

# The suggestion list of the tag search: options anywhere in the dialog (the
# member tag picker has no listbox around them, measured).
assert "querySelectorAll('[role=\"listbox\"]')].filter(vis);" in _OPTIONS_JS
_TAG_OPTIONS_JS = _OPTIONS_JS.replace(
    "const boxes = [...document.querySelectorAll('[role=\"listbox\"]')].filter(vis);",
    "const boxes = [...document.querySelectorAll('[role=\"dialog\"], dialog')].filter(vis);",
)
_TAG_PICK_JS = r"""(index) => {
  document.querySelectorAll('[data-ext-mention]').forEach(e => e.removeAttribute('data-ext-mention'));
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const opts = [...document.querySelectorAll('[role="dialog"], dialog')].filter(vis)
      .flatMap(b => [...b.querySelectorAll('[role="option"]')]).filter(vis);
  if (!opts[index]) return false;
  opts[index].setAttribute('data-ext-mention', 'pick');
  return true;
}"""


class MediaAttacher:
    """Upload, describe and tag images in an open composer."""

    def __init__(
        self,
        page: Any,
        *,
        editor_selector: str,
        media_words: list[str],
        allow_tags: bool,
        poll: float = 0.5,
        timeout: float = 20.0,
    ) -> None:
        self._page = page
        self._editor = editor_selector
        self._media_words = media_words
        self._allow_tags = allow_tags
        self._poll = poll
        self._timeout = timeout

    def _arg(self, **extra: Any) -> dict[str, Any]:
        return {
            "alt": words("alt_text"),
            "tag": words("tag_people"),
            "anchors": words("alt_text", "back"),
            **extra,
        }

    async def _state(self) -> dict[str, Any]:
        return await self._page.evaluate(_EDITOR_JS, self._arg()) or {}

    async def _click(self, tag: str, *, words_: list[str] | None = None,
                     thumb: int | None = None, prefix: bool = False,
                     scope: str = "editor") -> dict[str, Any]:
        found = await self._page.evaluate(
            _MARK_JS,
            self._arg(words=words_ or [], thumb=thumb, prefix=prefix, tag=tag, scope=scope),
        ) or {}
        if found.get("count") == 1 and not found.get("disabled"):
            await self._page.click(f'[data-ext-media="{tag}"]')
            await asyncio.sleep(self._poll * 2)
        return found

    async def _wait(self, check: Any) -> Any:
        loop = asyncio.get_running_loop()
        end = loop.time() + self._timeout
        while True:
            got = await check()
            if got or loop.time() >= end:
                return got
            await asyncio.sleep(self._poll)

    async def attach(self, items: list[dict[str, Any]], *, navigate: Any) -> dict[str, Any]:
        """Run the whole media step; ``{"status": "attached", ...}`` or a stop."""
        if any(m["tags"] for m in items) and not self._allow_tags:
            return {
                "status": "media_tag_unverifiable",
                "message": "The page composer's tag suggestions carry no identifier "
                "(measured); a tag by name alone could tag a namesake.",
            }
        wanted = [t for m in items for t in m["tags"]]
        if wanted:
            bad = await resolve_targets(self._page, navigate, wanted)
            if bad:
                return bad
        # 1. upload, in order
        opener = await self._page.evaluate(
            _MARK_JS,
            self._arg(
                words=self._media_words,
                tag="media-open",
                scope="composer",
                composer=self._editor,
            ),
        ) or {}
        if opener.get("count") != 1:
            return {"status": "media_button_unavailable", "found": opener.get("count")}
        async with self._page.expect_file_chooser(timeout=15_000) as info:
            await self._page.click('[data-ext-media="media-open"]')
        chooser = await info.value
        await chooser.set_files([str(m["path"]) for m in items])

        async def loaded() -> dict[str, Any] | None:
            st = await self._state()
            return st if st.get("editor") and len(st.get("thumbs") or []) >= len(items) else None

        state = await self._wait(loaded)
        if not state:
            got = await self._state()
            return {"status": "media_upload_incomplete",
                    "thumbnails": len(got.get("thumbs") or []), "wanted": len(items)}
        thumbs = state["thumbs"]
        if len(thumbs) != len(items):
            return {"status": "media_count_mismatch", "thumbnails": len(thumbs), "wanted": len(items)}
        # Order: the page composer names thumbnails by file, the member
        # composer by position -- either must match the request.
        names = [m["path"].name for m in items]
        by_position = [f"image {i}" for i in range(len(items))]
        if thumbs not in (names, by_position):
            return {"status": "media_order_mismatch", "shown": thumbs, "wanted": names}

        done: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            rec: dict[str, Any] = {"file": item["path"].name}
            if item["alt_text"]:
                got = await self._alt_text(i, item["alt_text"])
                if got["status"] != "ok":
                    return {**got, "index": i, "media": done}
                rec["alt_text"] = "verified"
            if item["tags"]:
                got = await self._tags(i, item["tags"])
                if got["status"] != "ok":
                    return {**got, "index": i, "media": done}
                rec["tags"] = got["tagged"]
            done.append(rec)

        nxt = await self._click("media-next", words_=words("next"))
        if nxt.get("count") != 1:
            return {"status": "image_editor_stuck", "media": done}

        async def previews() -> int | None:
            n = await self._page.evaluate(_COMPOSER_PREVIEWS_JS, {"editor": self._editor})
            return n if n == len(items) else None

        if not await self._wait(previews):
            n = await self._page.evaluate(_COMPOSER_PREVIEWS_JS, {"editor": self._editor})
            return {"status": "media_upload_incomplete", "previews": n,
                    "wanted": len(items), "media": done}
        return {"status": "attached", "media": done}

    async def _select(self, index: int) -> dict[str, Any]:
        got = await self._click(f"thumb-{index}", thumb=index)
        if got.get("count") != 1:
            return {"status": "media_thumbnail_unavailable"}
        return {"status": "ok"}

    async def _alt_text(self, index: int, text: str) -> dict[str, Any]:
        sel = await self._select(index)
        if sel["status"] != "ok":
            return sel
        opened = await self._click("alt-open", words_=words("alt_text"))
        if opened.get("count") != 1:
            return {"status": "alt_text_control_unavailable"}
        area = await self._page.evaluate(_TEXTAREA_JS) or {}
        if area.get("count") != 1:
            return {"status": "alt_text_control_unavailable"}
        await self._page.fill('[data-ext-media="alt-area"]', text)
        area = await self._page.evaluate(_TEXTAREA_JS) or {}
        if area.get("value") != text:
            await self._click("alt-back", words_=words("back"))
            return {"status": "alt_text_not_taken", "shown": (area.get("value") or "")[:80]}
        ok = await self._click("alt-ok", words_=words("media_confirm"))
        if ok.get("count") != 1 or ok.get("disabled"):
            await self._click("alt-back", words_=words("back"))
            return {"status": "alt_text_confirm_unavailable"}
        # Read-back: open it again; the saved text must be there.
        sel = await self._select(index)
        if sel["status"] != "ok":
            return sel
        await self._click("alt-open", words_=words("alt_text"))
        area = await self._page.evaluate(_TEXTAREA_JS) or {}
        back = await self._click("alt-back", words_=words("back"))
        if area.get("value") != text:
            return {"status": "alt_text_not_saved", "shown": (area.get("value") or "")[:80]}
        if back.get("count") != 1:
            return {"status": "image_editor_stuck"}
        return {"status": "ok"}

    async def _tags(self, index: int, tags: list[Mention]) -> dict[str, Any]:
        sel = await self._select(index)
        if sel["status"] != "ok":
            return sel
        before = (await self._state()).get("tag_label")
        opened = await self._click("tag-open", words_=words("tag_people"), prefix=True)
        if opened.get("count") != 1:
            return {"status": "media_tag_control_unavailable"}
        tagged: list[str] = []
        for tag in tags:
            box = await self._page.evaluate(_TAG_INPUT_JS, {"words": words("tag_input")}) or {}
            if box.get("count") != 1:
                await self._click("tag-back", words_=words("back"))
                return {"status": "media_tag_control_unavailable"}
            await self._page.fill('[data-ext-media="tag-input"]', "")
            await self._page.keyboard.type(tag.name, delay=60)
            listed = await self._settled_tag_options()
            if listed is None:
                await self._click("tag-back", words_=words("back"))
                return {"status": "mention_list_not_loaded", "mention": tag.name}
            choice = choose_option(listed, tag)
            if choice["status"] != "ok" or choice.get("verify_only"):
                # No read-back of a tag's target exists: a blind namesake is
                # refused here, unlike in the editor.
                await self._click("tag-back", words_=words("back"))
                status = choice["status"] if choice["status"] != "ok" else "media_tag_unverifiable"
                return {**choice, "status": status}
            if not await self._page.evaluate(_TAG_PICK_JS, choice["index"]):
                await self._click("tag-back", words_=words("back"))
                return {"status": "mention_list_not_loaded", "mention": tag.name}
            await self._page.click('[data-ext-mention="pick"]')
            await asyncio.sleep(self._poll * 2)
            tagged.append(tag.entity_id or tag.name)
        ok = await self._click("tag-ok", words_=words("media_confirm"))
        if ok.get("count") != 1 or ok.get("disabled"):
            await self._click("tag-back", words_=words("back"))
            return {"status": "media_tag_confirm_unavailable"}
        after = (await self._state()).get("tag_label") or ""
        count = _count(after) - _count(before or "")
        if count != len(tags):
            return {"status": "media_tags_not_saved", "shown": after}
        return {"status": "ok", "tagged": tagged}

    async def _settled_tag_options(self) -> list[dict[str, Any]] | None:
        loop = asyncio.get_running_loop()
        end = loop.time() + self._timeout
        last: Any = None
        same = 0
        while loop.time() < end:
            await asyncio.sleep(self._poll)
            snap = await self._page.evaluate(
                _TAG_OPTIONS_JS, {"company_words": words("company_hint")}, isolated_context=False
            ) or {}
            opts = snap.get("options") or []
            sig = [(o.get("title"), o.get("entity")) for o in opts]
            same = same + 1 if sig and sig == last else 0
            last = sig
            if same >= 2:
                return opts
        return None


def _count(label: str) -> int:
    import re

    m = re.search(r"(\d+)", label or "")
    return int(m.group(1)) if m else 0


__all__ = ["MediaAttacher", "check_media", "describe", "fold"]
