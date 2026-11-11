"""Fork extension: the CV as a file (2026-11-11).

``get_profile_cv`` reads the signed-in member's profile into one structured
document; ``apply_profile_cv`` takes a target document, diffs it against what
LinkedIn shows, plans the steps, dry-runs them, and with ``dry_run=false``
writes them one by one -- each booked, its pre-image stored in the local
ledger, read back, and the run stopped at the first step that does not read
back. ``edit_profile_section`` is the same step for a single item.

Rules carried over from ``edit_profile_intro``: identity gate ``expected_slug``
before any field is touched, dry run by default, ``already_attempted`` for an
identical open change, pace kind ``profile_cv_edit`` (30/day, 60/week; the
intro and featured stay on ``profile_edit``). Deletions only with
``allow_delete=true``. A section whose current items cannot be read as
structured values is never written: a create there could duplicate an item.
"""

from __future__ import annotations

import json
from typing import Any

import yaml
from fastmcp import Context

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.linkedin.ext_profile_cv import (
    SECTIONS,
    SINGLETONS,
    ExtProfileCv,
    item_key,
    norm,
    parse_date,
)
from linkedin_mcp_server.tools.ext import ToolRegistrar
from linkedin_mcp_server.tools.ext_profile import (
    HEADLINE_MAX,
    _operate,
    change_key,
    check_line,
    check_slug,
    check_url,
)

TAG = "ext"
CV_KIND = "profile_cv_edit"
TEXT_MAX = 2000
LINE_MAX = 255
MAX_ITEMS = 60
UNSUPPORTED = {
    "skills": "the skills add/edit form is not measured (no dialog at /edit/forms/skill/new/)",
    "website": "contact info edit is not measured (no dialog at /edit/contact-info/)",
    "open_to": "read only: LinkedIn's open-to flags are not written by this server",
    "language_profiles": "secondary-language profiles are not measured yet",
}
_URL_FIELDS = {"credential_url", "url"}
_DATE_FIELDS = {"start", "end", "issued", "expires", "date"}
_AREA_FIELDS = {"description", "activities", "about"}


def load_cv(text: str) -> dict[str, Any]:
    """YAML or JSON text -> dict; ValueError with a reason otherwise."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"cv is neither YAML nor JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("cv must be a mapping of sections")
    return data


def check_cv(cv: dict[str, Any]) -> dict[str, Any] | None:
    """Field-level validation; an invalid_input status or None."""
    known = set(SECTIONS) | set(SINGLETONS) | set(UNSUPPORTED) | {"headline", "profile"}
    unknown = sorted(set(cv) - known)
    if unknown:
        return {
            "status": "invalid_input",
            "field": "cv",
            "message": f"unknown sections {unknown}",
        }
    if cv.get("headline") is not None and (
        bad := check_line(cv["headline"], "headline", HEADLINE_MAX)
    ):
        return bad
    if cv.get("about") is not None and (
        not isinstance(cv["about"], str) or len(cv["about"]) > 2600
    ):
        return {
            "status": "invalid_input",
            "field": "about",
            "message": "text, max 2600",
        }
    loc = cv.get("location")
    if loc is not None and (
        not isinstance(loc, dict) or set(loc) - {"country", "city"}
    ):
        return {
            "status": "invalid_input",
            "field": "location",
            "message": "{country, city}",
        }
    for section, spec in SECTIONS.items():
        items = cv.get(section)
        if items is None:
            continue
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            return {
                "status": "invalid_input",
                "field": section,
                "message": f"list, max {MAX_ITEMS}",
            }
        seen: set[tuple[str, ...]] = set()
        for i, item in enumerate(items):
            where = f"{section}[{i}]"
            if isinstance(item, str) and len(spec["key"]) == 1:
                item = {spec["key"][0]: item}
                items[i] = item
            if not isinstance(item, dict):
                return {"status": "invalid_input", "field": where}
            extra = sorted(set(item) - set(spec["fields"]) - {"skills"})
            if extra:
                return {
                    "status": "invalid_input",
                    "field": where,
                    "message": f"unknown fields {extra}",
                }
            if not all(item.get(k) for k in spec["key"]):
                return {
                    "status": "invalid_input",
                    "field": where,
                    "message": f"needs {list(spec['key'])}",
                }
            key = item_key(section, item)
            if key in seen:
                return {
                    "status": "invalid_input",
                    "field": where,
                    "message": "duplicate item key",
                }
            seen.add(key)
            for name, value in item.items():
                if value is None or name == "skills":
                    continue
                kind = spec["fields"][name][0]
                try:
                    if name in _DATE_FIELDS:
                        parse_date(str(value))
                except ValueError as exc:
                    return {
                        "status": "invalid_input",
                        "field": f"{where}.{name}",
                        "message": str(exc),
                    }
                if kind == "check":
                    if not isinstance(value, bool):
                        return {
                            "status": "invalid_input",
                            "field": f"{where}.{name}",
                            "message": "true/false",
                        }
                elif name in _URL_FIELDS:
                    if bad := check_url(str(value), f"{where}.{name}"):
                        return bad
                elif name in _DATE_FIELDS:
                    pass
                elif name in _AREA_FIELDS:
                    if not isinstance(value, str) or len(value) > TEXT_MAX:
                        return {
                            "status": "invalid_input",
                            "field": f"{where}.{name}",
                            "message": f"max {TEXT_MAX}",
                        }
                elif bad := check_line(str(value), f"{where}.{name}", LINE_MAX):
                    return bad
    return None


def _differs(want: dict[str, Any], have: dict[str, Any]) -> list[str]:
    out = []
    for name, value in want.items():
        if value is None or name == "skills":
            continue
        got = have.get(name)
        if isinstance(value, bool):
            if bool(got) != value:
                out.append(name)
        elif name in _DATE_FIELDS:
            if norm(got) != norm(value):
                out.append(name)
        elif norm(got) != norm(value):
            out.append(name)
    return out


def plan_cv(
    target: dict[str, Any], current: dict[str, Any], *, allow_delete: bool
) -> dict[str, Any]:
    """Diff a target CV against the current one: ordered steps plus what is skipped.

    ``current`` is ``get_profile_cv``'s ``cv``; per list section a list of
    ``{"values": {...} | None, "href": str | None}``.
    """
    steps: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if target.get("headline") is not None and norm(target["headline"]) != norm(
        (current.get("headline") or {}).get("headline")
    ):
        steps.append(
            {
                "op": "intro",
                "section": "headline",
                "values": {"headline": target["headline"]},
            }
        )
    for single in SINGLETONS:
        want = target.get(single)
        if want is None:
            continue
        want = {"about": want} if single == "about" else dict(want)
        have = current.get(single) or {}
        if _differs(want, have):
            steps.append(
                {
                    "op": "update",
                    "section": single,
                    "values": want,
                    "fields": _differs(want, have),
                }
            )
    for section in SECTIONS:
        wanted = target.get(section)
        if wanted is None:
            continue
        have = current.get(section)
        if not isinstance(have, list) or any(
            i.get("values") is None or not i.get("href") for i in have
        ):
            skipped.append(
                {
                    "section": section,
                    "reason": "current_unstructured",
                    "detail": "current items not readable as values; nothing is written here",
                }
            )
            continue
        by_key: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for item in have:
            by_key.setdefault(item_key(section, item["values"]), []).append(item)
        ambiguous = [k for k, v in by_key.items() if len(v) > 1]
        if ambiguous:
            skipped.append(
                {
                    "section": section,
                    "reason": "current_duplicate_key",
                    "keys": ambiguous,
                }
            )
            continue
        wanted_keys = set()
        for item in wanted:
            key = item_key(section, item)
            wanted_keys.add(key)
            values = {k: v for k, v in item.items() if k != "skills"}
            if item.get("skills"):
                skipped.append(
                    {
                        "section": section,
                        "key": list(key),
                        "reason": "skills_not_measured",
                    }
                )
            if key in by_key:
                cur = by_key[key][0]
                fields = _differs(values, cur["values"])
                if fields:
                    steps.append(
                        {
                            "op": "update",
                            "section": section,
                            "key": list(key),
                            "href": cur["href"],
                            "values": values,
                            "fields": fields,
                        }
                    )
            else:
                steps.append(
                    {
                        "op": "create",
                        "section": section,
                        "key": list(key),
                        "values": values,
                    }
                )
        for key, items in by_key.items():
            if key in wanted_keys:
                continue
            if allow_delete:
                steps.append(
                    {
                        "op": "delete",
                        "section": section,
                        "key": list(key),
                        "href": items[0]["href"],
                        "pre_image": items[0]["values"],
                    }
                )
            else:
                skipped.append(
                    {
                        "section": section,
                        "key": list(key),
                        "reason": "delete_not_allowed",
                    }
                )
    for section in UNSUPPORTED:
        if target.get(section) is not None:
            skipped.append(
                {
                    "section": section,
                    "reason": "unsupported",
                    "detail": UNSUPPORTED[section],
                }
            )
    # Creates before deletes: a section is never emptier than both states.
    order = {"intro": 0, "update": 1, "create": 2, "delete": 3}
    steps.sort(key=lambda s: order[s["op"]])
    return {"steps": steps, "skipped": skipped}


def step_key(slug: str, step: dict[str, Any]) -> str:
    return change_key(
        op=f"cv-{step['op']}",
        slug=slug,
        section=step["section"],
        key=repr(step.get("key")),
        href=step.get("href"),
        values=json.dumps(step.get("values"), sort_keys=True, ensure_ascii=False),
    )


async def read_cv(
    editor: ExtProfileCv, slug: str, sections: list[str]
) -> dict[str, Any]:
    cv: dict[str, Any] = {}
    if "headline" in sections:
        intro = await editor.read_intro(slug)
        cv["headline"] = {"headline": intro.get("headline")}
    for single in SINGLETONS:
        if single in sections:
            got = await editor.read_item(slug, single, None)
            cv[single] = got["values"]
    for section in SECTIONS:
        if section not in sections:
            continue
        page = await editor.list_items(slug, section)
        items = []
        for item in page.get("items") or []:
            got = await editor.read_item(slug, section, item["href"])
            items.append(
                {"href": item["href"], "values": got["values"], "lines": item["lines"]}
            )
        cv[section] = items
    return cv


def to_target(cv: dict[str, Any]) -> dict[str, Any]:
    """A read CV in the file form ``apply_profile_cv`` takes back unchanged."""
    out: dict[str, Any] = {}
    if (cv.get("headline") or {}).get("headline"):
        out["headline"] = cv["headline"]["headline"]
    if (cv.get("about") or {}).get("about"):
        out["about"] = cv["about"]["about"]
    if cv.get("location"):
        out["location"] = cv["location"]
    for section in SECTIONS:
        if isinstance(cv.get(section), list):
            out[section] = [i["values"] for i in cv[section] if i.get("values")]
    return out


def register_ext_profile_cv_tools(
    mcp: ToolRegistrar, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    from linkedin_mcp_server.tools.ext import _GuardedMcp, _pace, _run

    mcp = _GuardedMcp(mcp)  # type: ignore[assignment]
    timeout = max(tool_timeout, 600.0)
    all_sections = ["headline", *SINGLETONS, *SECTIONS]

    @mcp.tool(
        timeout=timeout,
        title="Get Profile CV",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={TAG, "profile"},
    )
    async def get_profile_cv(
        expected_slug: str,
        ctx: Context,
        sections: list[str] | None = None,
        output: str = "yaml",
    ) -> dict[str, Any]:
        """
        Read the signed-in member's profile as a CV document: headline, about,
        location and the list sections experience, education, certifications,
        languages, projects, volunteering, honors, publications. Each list item
        is read from its own edit form (opened and discarded, nothing saved),
        so values are exact; ``href`` is the item's edit form. Identity gate as
        in edit_profile_intro. ``cv_file`` is the same content in the file form
        apply_profile_cv takes (output: yaml or json).

        Statuses: read, wrong_account, own_identity_unknown, invalid_input,
        pace_budget_spent.
        """
        if bad := check_slug(expected_slug):
            return bad
        wanted = sections or all_sections
        if set(wanted) - set(all_sections) or output not in ("yaml", "json"):
            return {
                "status": "invalid_input",
                "field": "sections/output",
                "allowed": all_sections,
            }
        slug = expected_slug.strip().lower()

        async def body(ex: Any) -> dict[str, Any]:
            spent = _pace("page_read", 3 + 2 * len(wanted), tool="get_profile_cv")
            if spent:
                return spent
            editor = ExtProfileCv(ex.ext_session, ex.ext_navigator)
            if denied := await editor.check_identity(slug):
                return {"expected_slug": slug, **denied}
            cv = await read_cv(editor, slug, wanted)
            target = to_target(cv)
            text = (
                yaml.safe_dump(target, allow_unicode=True, sort_keys=False)
                if output == "yaml"
                else json.dumps(target, ensure_ascii=False, indent=2)
            )
            return {
                "status": "read",
                "expected_slug": slug,
                "cv": cv,
                "cv_file": text,
                "not_covered": UNSUPPORTED,
            }

        return await _run(ctx, "get_profile_cv", body)

    @mcp.tool(
        timeout=timeout,
        title="Edit Profile Section",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "profile", "actions"},
    )
    async def edit_profile_section(
        expected_slug: str,
        section: str,
        ctx: Context,
        values: dict[str, Any] | None = None,
        item_href: str | None = None,
        delete: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """
        Create, change or delete one item of a CV section (experience,
        education, certifications, languages, projects, volunteering, honors,
        publications) or set about/location. item_href (from get_profile_cv)
        names an existing item; without it a new item is created. delete=true
        removes the named item. Identity gate and dry run as in
        edit_profile_intro: the dialog is filled and compared, then discarded.
        The "notify network" switch is held off, and the checkbox that would
        end the current position is never touched.

        Values: dates as YYYY-MM (or YYYY); current: true/false; selects by
        their visible option text (e.g. employment_type "Vollzeit").

        Statuses: dry_run, verified, unverified, unchanged, wrong_account,
        own_identity_unknown, form_missing, add_control_missing,
        field_not_unique (with the form inventory), value_not_offered,
        editor_mismatch, guard_tripped, save_button_unavailable,
        delete_control_not_unique, already_attempted, pace_budget_spent
        (profile_cv_edit: 30/day, 60/week), invalid_input.
        """
        if bad := check_slug(expected_slug):
            return bad
        if section not in SECTIONS and section not in SINGLETONS:
            return {
                "status": "invalid_input",
                "field": "section",
                "allowed": [*SECTIONS, *SINGLETONS],
            }
        if item_href is not None and (
            "/edit/forms/" not in item_href
            or not item_href.startswith(("/in/", "https://www.linkedin.com/in/"))
        ):
            return {"status": "invalid_input", "field": "item_href"}
        if delete and (section in SINGLETONS or not item_href):
            return {
                "status": "invalid_input",
                "field": "delete",
                "message": "needs item_href of a list item",
            }
        if not delete:
            if not values:
                return {"status": "nothing_to_change"}
            probe = (
                {section: [dict(values)]}
                if section in SECTIONS
                else {section: values.get("about") if section == "about" else values}
            )
            if (
                section in SECTIONS and item_href
            ):  # key fields may be absent on an update
                spec = SECTIONS[section]
                probe[section][0] = {**{k: "x" for k in spec["key"]}, **values}
            if bad := check_cv(probe):
                return bad
        slug = expected_slug.strip().lower()
        step = {
            "op": "delete" if delete else ("update" if item_href else "create"),
            "section": section,
            "href": item_href,
            "values": values,
        }
        target = {
            "expected_slug": slug,
            "section": section,
            "change": step_key(slug, step),
        }

        async def action(editor: Any, confirm: bool) -> dict[str, Any]:
            return await editor.write_item(
                slug,
                section,
                href=item_href,
                values=values,
                delete=delete,
                confirm=confirm,
            )

        return await _operate(
            ctx,
            tool="edit_profile_section",
            target=target,
            dry_run=dry_run,
            action=action,
            kind=CV_KIND,
            editor_cls=ExtProfileCv,
        )

    @mcp.tool(
        timeout=timeout,
        title="Apply Profile CV",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={TAG, "profile", "actions"},
    )
    async def apply_profile_cv(
        expected_slug: str,
        cv: str,
        ctx: Context,
        allow_delete: bool = False,
        dry_run: bool = True,
        max_steps: int = 12,
    ) -> dict[str, Any]:
        """
        Bring the signed-in member's profile to a target CV (YAML or JSON text,
        same form as get_profile_cv's cv_file). Reads the current profile,
        diffs, and plans steps: update changed items, create missing ones,
        delete surplus ones only with allow_delete=true (otherwise reported as
        skipped). Items match by key: experience (title, company), education
        (school, degree), certifications (name, issuer), languages/projects/
        honors/publications (title or name), volunteering (organization, role).

        Dry run (default): every planned step's dialog is filled, compared and
        discarded -- no save. Live (dry_run=false): steps run in order, each
        booked separately (pace profile_cv_edit), its pre-image stored in the
        local ledger and read back; the run stops at the first step that is
        not verified or unchanged. Running it again after a stop continues
        where it stopped, because the plan is made from what LinkedIn shows.

        Statuses: planned (dry run), applied, stopped (see failed_step),
        nothing_to_change, too_many_steps, wrong_account,
        own_identity_unknown, invalid_input, pace_budget_spent.
        """
        if bad := check_slug(expected_slug):
            return bad
        if not 1 <= max_steps <= 30:
            return {"status": "invalid_input", "field": "max_steps", "message": "1-30"}
        try:
            target = load_cv(cv)
        except ValueError as exc:
            return {"status": "invalid_input", "field": "cv", "message": str(exc)}
        if bad := check_cv(target):
            return bad
        slug = expected_slug.strip().lower()
        sections = [
            s for s in ["headline", *SINGLETONS, *SECTIONS] if target.get(s) is not None
        ]

        async def read(ex: Any) -> dict[str, Any]:
            spent = _pace("page_read", 3 + 2 * len(sections), tool="apply_profile_cv")
            if spent:
                return spent
            editor = ExtProfileCv(ex.ext_session, ex.ext_navigator)
            if denied := await editor.check_identity(slug):
                return denied
            return {"status": "read", "cv": await read_cv(editor, slug, sections)}

        got = await _run(ctx, "apply_profile_cv", read)
        if got.get("status") != "read":
            return {"expected_slug": slug, **got}
        plan = plan_cv(target, got["cv"], allow_delete=allow_delete)
        if not plan["steps"]:
            return {"status": "nothing_to_change", "expected_slug": slug, **plan}
        if len(plan["steps"]) > max_steps:
            return {
                "status": "too_many_steps",
                "expected_slug": slug,
                "max_steps": max_steps,
                **plan,
            }
        results = []
        for step in plan["steps"]:
            result = await _run_step(ctx, slug, step, dry_run)
            results.append({"step": step, "result": result})
            ok = ("dry_run", "unchanged") if dry_run else ("verified", "unchanged")
            if result.get("status") not in ok:
                return {
                    "status": "stopped",
                    "expected_slug": slug,
                    "dry_run": dry_run,
                    "failed_step": step,
                    "results": results,
                    "skipped": plan["skipped"],
                }
        return {
            "status": "planned" if dry_run else "applied",
            "expected_slug": slug,
            "dry_run": dry_run,
            "results": results,
            "skipped": plan["skipped"],
        }

    async def _run_step(
        ctx: Context, slug: str, step: dict[str, Any], dry_run: bool
    ) -> dict[str, Any]:
        target = {"expected_slug": slug, "change": step_key(slug, step)}
        if step["op"] == "intro":

            async def intro(editor: Any, confirm: bool) -> dict[str, Any]:
                return await editor.edit_intro(
                    slug,
                    button_url=None,
                    button_text=None,
                    headline=step["values"]["headline"],
                    confirm=confirm,
                )

            return await _operate(
                ctx,
                tool="apply_profile_cv",
                target=target,
                dry_run=dry_run,
                action=intro,
            )

        async def action(editor: Any, confirm: bool) -> dict[str, Any]:
            return await editor.write_item(
                slug,
                step["section"],
                href=step.get("href"),
                values=step.get("values"),
                delete=step["op"] == "delete",
                confirm=confirm,
            )

        return await _operate(
            ctx,
            tool="apply_profile_cv",
            target=target,
            dry_run=dry_run,
            action=action,
            kind=CV_KIND,
            editor_cls=ExtProfileCv,
        )
