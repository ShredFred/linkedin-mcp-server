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
    "start_post_media": {"de": ["mediendatei hinzufügen", "medieninhalte", "foto hinzufügen"], "en": ["add media"]},
    "discard": {"de": ["verwerfen", "änderungen verwerfen"], "en": ["discard"]},
    "save": {
        "de": ["speichern", "änderungen speichern"],
        "en": ["save", "save changes"],
    },
    "cancel": {"de": ["abbrechen"], "en": ["cancel"]},
    # measured 2026-10-09 (de): "Mediendatei hinzufügen"
    "media": {
        # measured: "Medieninhalte" (member), "Mediendatei hinzufügen" (page)
        "de": ["mediendatei hinzufügen", "medieninhalte", "medien", "foto", "foto hinzufügen"],
        "en": ["add media", "media", "photo", "add a photo"],
    },
    "video": {"de": ["video hinzufügen", "video"], "en": ["add a video", "video"]},
    # measured 2026-10-09 (de, page composer under "Mehr")
    "document": {
        "de": ["dokument hinzufügen", "dokument"],
        "en": ["add a document", "document"],
    },
    "more_options": {
        "de": ["mehr", "weitere optionen"],
        "en": ["more", "more options"],
    },
    # The image editor: alternative text and person tags.
    # measured 2026-10-09 (de), both composers: "Alternativer Text", "Tag"
    # (the tag button reads "Tag, 1 Person getaggt" once someone is tagged).
    "alt_text": {
        "de": ["alternativer text", "alternativtext", "alt-text"],
        "en": ["alternative text", "alt text"],
    },
    "tag_people": {"de": ["tag", "personen markieren"], "en": ["tag", "tag people"]},
    # Confirms the alt-text or tag sub-dialog: "Hinzufügen", on a second
    # visit "Aktualisieren"; the tag list re-opened shows "Speichern".
    "media_confirm": {
        "de": ["hinzufügen", "aktualisieren", "speichern"],
        "en": ["add", "update", "save"],
    },
    "back": {"de": ["zurück"], "en": ["back"]},
    "tag_input": {"de": ["namen eingeben"], "en": ["enter a name", "type a name"]},
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
    # Page composer (measured 2026-10-09, de): schedule clock, its commit
    # "Planen", the time list, the draft prompt.
    "schedule": {
        "de": ["termin für beitrag festlegen", "termin fuer beitrag festlegen", "beitrag planen"],
        "en": ["schedule post"],
    },
    "schedule_commit": {"de": ["planen"], "en": ["schedule"]},
    "expand_time": {"de": ["zeitauswahl erweitern"], "en": ["expand time selection"]},
    "draft": {
        "de": ["als entwurf speichern", "entwurf speichern", "speichern"],
        "en": ["save as draft", "save draft"],
    },
    # Own posts and comments: menu entries and the delete confirmation.
    "edit_post": {"de": ["beitrag bearbeiten", "bearbeiten"], "en": ["edit post", "edit"]},
    "delete_post": {"de": ["beitrag löschen", "löschen"], "en": ["delete post", "delete"]},
    "edit_comment": {"de": ["kommentar bearbeiten", "bearbeiten"], "en": ["edit comment", "edit"]},
    "delete_comment": {"de": ["kommentar löschen", "löschen"], "en": ["delete comment", "delete"]},
    "confirm_delete": {
        "de": ["löschen", "beitrag löschen", "kommentar löschen"],
        "en": ["delete", "delete post", "delete comment"],
    },
    # Submit of a comment box (measured de: "Kommentieren", admin view).
    "comment_submit": {"de": ["kommentieren"], "en": ["comment"]},
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
