"""Every status a fork tool can return is named in its docstring.

Claude agents see only the tool description, i.e. the docstring. A status the
code returns but the docstring never names is one the caller cannot handle on
purpose -- it guesses, and for a write tool a wrong guess means a resend.

The check is static (AST) and deliberately pragmatic: inside each tool
function and every module-level helper it calls directly or transitively in
the same module (plus helpers imported from ``tools.ext``), it collects
dict literals ``{"status": "<constant>"}``, ``dict(status="<constant>")`` and
``x["status"] = "<constant>"``. Statuses produced deeper (extractor classes,
``ext_outreach``) are not seen; that is a known blind spot, not a promise.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "linkedin_mcp_server" / "tools"
MODULES = ("ext", "ext_stage2", "ext_own_content", "ext_inmail")

# Statuses that need not be repeated in every single docstring, with reason.
GLOBAL_EXCEPTIONS = {
    # Added by _GuardedMcp around every fork tool; not tool-specific. It is
    # documented once in the README fork section.
    "ledger_corrupt": "set by the _GuardedMcp wrapper for every tool",
}

# (tool, status) pairs that are unreachable or deliberately undocumented.
_NO_QUOTA = (
    "_book_attempt maps _CampaignQuotaReached, which only the quota_check of "
    "send_campaign_batch raises; this tool passes none"
)
_DUP_RENAMED = "_book_attempt's duplicate is renamed before it is returned"
_LEDGER_ONLY = "status of the ledger row only; the response carries the page result"
TOOL_EXCEPTIONS: dict[tuple[str, str], str] = {
    **{
        (tool, "campaign_quota_reached"): _NO_QUOTA
        for tool in (
            "connect_guarded",
            "create_post",
            "send_message_verified",
            "comment_on_post",
            "delete_own_comment",
            "delete_own_post",
            "edit_own_comment",
            "edit_own_post",
            "edit_sent_message",
            "send_inmail",
            "repost_post",
        )
    },
    ("repost_post", "duplicate"): _DUP_RENAMED + " (already_reposted / repost_pending)",
    ("repost_post", "not_done"): _LEDGER_ONLY,
    ("create_post", "duplicate"): _DUP_RENAMED + " (duplicate_text)",
    ("comment_on_post", "duplicate"): _DUP_RENAMED + " (duplicate_text)",
    **{
        (tool, "duplicate"): _DUP_RENAMED + " (already_attempted)"
        for tool in (
            "delete_own_comment",
            "delete_own_post",
            "edit_own_comment",
            "edit_own_post",
        )
    },
    ("edit_sent_message", "duplicate"): (
        "_book_attempt gets no duplicate check here; unreachable"
    ),
    ("comment_on_post", "not_posted"): _LEDGER_ONLY,
    **{
        (tool, "not_done"): _LEDGER_ONLY
        for tool in (
            "delete_own_comment",
            "delete_own_post",
            "edit_own_comment",
            "edit_own_post",
        )
    },
    ("delete_own_post", "invalid_comment"): (
        "resolve_target is called with comment_id=None for posts; unreachable"
    ),
    ("edit_own_post", "invalid_comment"): (
        "resolve_target is called with comment_id=None for posts; unreachable"
    ),
}


def _parse(name: str) -> ast.Module:
    return ast.parse((TOOLS / f"{name}.py").read_text(encoding="utf-8"))


def _module_functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_tool(fn: ast.AST) -> bool:
    for deco in getattr(fn, "decorator_list", []):
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _tools(tree: ast.Module) -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    found = []
    for reg in _module_functions(tree).values():
        if not reg.name.startswith("register_"):
            continue
        for node in ast.walk(reg):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tool(
                node
            ):
                found.append(node)
    return found


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


# Dicts that are ledger rows, not tool results: arguments of these calls and
# values assigned to these names.
_LEDGER_CALLS = {"append", "record", "write_row"}
_LEDGER_NAMES = {"row", "entry"}
_LEDGER_FUNCS = {"_book_attempt"}


def _ledger_rows(fn: ast.AST) -> set[int]:
    skip: set[int] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LEDGER_CALLS
        ):
            skip.update(id(a) for a in node.args)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _LEDGER_FUNCS:
                skip.update(id(a) for a in node.args)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in _LEDGER_NAMES for t in targets):
                skip.add(id(node.value))
    return skip


def _consts(node: ast.AST | None) -> set[str]:
    """String constants a status expression can take: literal or a ? b : c."""
    if isinstance(node, ast.IfExp):
        return _consts(node.body) | _consts(node.orelse)
    s = _const_str(node)
    return {s} if s else set()


def _name_values(fn: ast.AST, name: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            out |= _consts(node.value)
    return out


def _value_statuses(fn: ast.AST, value: ast.AST | None) -> set[str]:
    if isinstance(value, ast.Name):
        return _name_values(fn, value.id)
    return _consts(value)


def _direct_statuses(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    skip = _ledger_rows(fn)
    for node in ast.walk(fn):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if _const_str(key) == "status":
                    out |= _value_statuses(fn, value)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "dict":
                for kw in node.keywords:
                    if kw.arg == "status":
                        out |= _value_statuses(fn, kw.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and _const_str(target.slice) == "status"
                ):
                    out |= _value_statuses(fn, node.value)
    return out


def _called_names(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
        # helpers passed by reference, e.g. return refusal(...) via a variable
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
    return names


def _helpers(module: str) -> dict[str, ast.AST]:
    helpers = dict(_module_functions(_parse("ext")))
    if module != "ext":
        helpers.update(_module_functions(_parse(module)))
    return {k: v for k, v in helpers.items() if not k.startswith("register_")}


def tool_statuses(module: str) -> dict[str, tuple[set[str], str]]:
    tree = _parse(module)
    helpers = _helpers(module)
    result: dict[str, tuple[set[str], str]] = {}
    for tool in _tools(tree):
        statuses = _direct_statuses(tool)
        seen: set[str] = set()
        todo = list(_called_names(tool))
        while todo:
            name = todo.pop()
            if name in seen or name not in helpers:
                continue
            seen.add(name)
            statuses |= _direct_statuses(helpers[name])
            todo.extend(_called_names(helpers[name]))
        result[tool.name] = (statuses, ast.get_docstring(tool) or "")
    return result


def _named(status: str, doc: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(status)}(?!\w)", doc) is not None


CASES = [
    (module, tool, status)
    for module in MODULES
    for tool, (statuses, _doc) in sorted(tool_statuses(module).items())
    for status in sorted(statuses)
]


def test_tools_found() -> None:
    assert sum(len(tool_statuses(m)) for m in MODULES) >= 30


@pytest.mark.parametrize(("module", "tool", "status"), CASES)
def test_status_named_in_docstring(module: str, tool: str, status: str) -> None:
    if status in GLOBAL_EXCEPTIONS or (tool, status) in TOOL_EXCEPTIONS:
        pytest.skip("documented exception")
    doc = tool_statuses(module)[tool][1]
    assert _named(status, doc), (
        f"{module}.{tool}: status {status!r} missing in docstring"
    )


def test_exceptions_are_not_stale() -> None:
    found = {
        (tool, status)
        for module in MODULES
        for tool, (statuses, _doc) in tool_statuses(module).items()
        for status in statuses
    }
    assert not set(TOOL_EXCEPTIONS) - found


def test_global_exceptions_documented_in_readme() -> None:
    readme = (TOOLS.parents[1] / "README.md").read_text(encoding="utf-8")
    for status in GLOBAL_EXCEPTIONS:
        assert status in readme, status


if __name__ == "__main__":  # report helper: python tests/test_ext_r6_doku.py
    for m in MODULES:
        for t, (st, d) in sorted(tool_statuses(m).items()):
            missing = sorted(
                s
                for s in st
                if not _named(s, d)
                and s not in GLOBAL_EXCEPTIONS
                and (t, s) not in TOOL_EXCEPTIONS
            )
            if missing:
                print(f"{m}.{t}: {', '.join(missing)}")
