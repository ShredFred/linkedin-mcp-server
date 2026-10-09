"""Fork extension: one bilingual label table for every composer control.

LinkedIn renders the same control with a German or an English label depending
on the member's interface language. Before this table each composer module
carried its own word lists, and a locale switch broke whichever list had been
written from a German-only measurement. Every entry here therefore holds both
languages, and ``test_ext_composer_labels.py`` refuses a key without one of
them.

Values are compared lower-cased and whitespace-collapsed, either against a
button's visible text (``words``) or its ``aria-label`` (``labels``). Entries
marked *measured* were read off the live page; the others are LinkedIn's
documented wording and fail closed (exactly one visible match or a status).
"""

from __future__ import annotations

LABELS: dict[str, dict[str, list[str]]] = {
    # measured 2026-09-29 (de), 2026-10-09 (de)
    "post": {
        "de": ["posten", "veröffentlichen"],
        "en": ["post", "publish"],
    },
    "next": {"de": ["weiter", "fertig"], "en": ["next", "done"]},
    "close": {"de": ["schließen", "verwerfen"], "en": ["close", "dismiss", "discard"]},
    "discard": {"de": ["verwerfen", "änderungen verwerfen"], "en": ["discard"]},
    "save": {
        "de": ["speichern", "änderungen speichern"],
        "en": ["save", "save changes"],
    },
    "cancel": {"de": ["abbrechen"], "en": ["cancel"]},
    # measured 2026-10-09 (de): "Mediendatei hinzufügen"
    "media": {
        "de": ["mediendatei hinzufügen", "medieninhalte", "medien", "foto", "foto hinzufügen"],
        "en": ["add media", "media", "photo", "add a photo"],
    },
    "video": {"de": ["video hinzufügen", "video"], "en": ["add a video", "video"]},
    "document": {
        "de": ["dokument hinzufügen", "dokument"],
        "en": ["add a document", "document"],
    },
    "more_options": {
        "de": ["mehr", "weitere optionen"],
        "en": ["more", "more options"],
    },
    # The image editor: alternative text and person tags.
    "alt_text": {
        "de": ["alternativtext", "alt-text", "alternativtext hinzufügen"],
        "en": ["alternative text", "alt text", "add alt text"],
    },
    "tag_people": {
        "de": ["personen markieren", "markieren"],
        "en": ["tag people", "tag"],
    },
    "apply": {"de": ["übernehmen", "anwenden", "speichern"], "en": ["apply", "save"]},
    "document_title": {
        "de": ["titel", "dokumenttitel"],
        "en": ["title", "document title"],
    },
    # Author selection ("Posten als" / "Post as"). Read, never the only proof.
    "post_as": {"de": ["posten als", "als"], "en": ["post as", "posting as"]},
    "start_post": {
        "de": ["beitrag beginnen", "beitrag erstellen"],
        "en": ["start a post", "create a post"],
    },
    # Typeahead option hints: a company option carries one of these words.
    "company_hint": {
        "de": ["unternehmen", "firma", "follower"],
        "en": ["company", "followers"],
    },
}


def words(*keys: str) -> list[str]:
    """Both languages of every key, lower case, de-duplicated, order kept."""
    out: list[str] = []
    for key in keys:
        entry = LABELS[key]
        for lang in ("de", "en"):
            for value in entry[lang]:
                if value not in out:
                    out.append(value)
    return out
