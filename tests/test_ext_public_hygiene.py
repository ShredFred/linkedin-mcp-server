"""Fork extension: the public tree carries no deployment identity.

The fork is published. Company, person and page identifiers of the deployment
it was built for live in the environment (LINKEDIN_MCP_CANARY,
LINKEDIN_MCP_CALENDLY_ACCOUNT, ...), never in the tree. The banned words are
kept as truncated SHA-256 hashes so that this test does not reintroduce them.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from linkedin_mcp_server import ext_message_checks as checks
from linkedin_mcp_server import ext_outreach as outreach

ROOT = Path(__file__).resolve().parents[1]

BANNED = {
    "d0957e5f0f22c6ef",  # company name
    "c41d8d203350af5a",  # operator first name
    "b04d42b1c0d56888",  # operator last name
    "1e9e5b9599bd1078",  # operator profile slug
    "e1fc45f7880e0505",  # member first name
    "c5f975b35c72cfe2",  # member last name
    "431c6266691b45ce",  # measured third party
    "1976e5726815ff81",  # measured third party
    "4448210384ccdb4d",  # measured third party slug
    "f8bb8720f1c9d2dd",  # measured company
    "ef4da2b2f27cb306",  # organization id
    "82fb2e785533ecdc",  # organization id
    "a3068d2f38f3b5db",  # association
    "914e76eb565ae305",  # measured third party
    "560e929de8415cac",  # measured third party
    "432535c48ba1bcec",  # measured third party slug suffix
}
_WORD = re.compile(r"[a-z0-9äöüß]+")


def _h(word: str) -> str:
    return hashlib.sha256(word.encode()).hexdigest()[:16]


def _tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [ROOT / f for f in out.split("\0") if f]


def test_no_deployment_identity_in_tracked_files() -> None:
    hits = []
    for path in _tracked():
        rel = path.relative_to(ROOT).as_posix()
        if any(_h(w) in BANNED for w in _WORD.findall(rel.lower())):
            hits.append(f"{rel} (path)")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for n, line in enumerate(text.lower().splitlines(), 1):
            if any(_h(w) in BANNED for w in _WORD.findall(line)):
                hits.append(f"{rel}:{n}")
    assert hits == []


def test_calendly_unset_refuses_every_calendly_link(monkeypatch) -> None:
    monkeypatch.delenv(checks.CALENDLY_ACCOUNT_ENV, raising=False)
    assert checks.calendly_account() == ""
    assert not checks.calendly_ok("https://calendly.com/acme_jane-doe/30min")
    assert [f["code"] for f in checks.check_links("https://calendly.com/x/30")] == [
        "calendly_wrong_account"
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "acme_jane-doe",
        "calendly.com/acme_jane-doe",
        "https://www.calendly.com/ACME_jane-doe/",
        " acme_jane-doe/30min ",
        "calendly.com/acme_jane-doe?month=2026-10#x",
    ],
)
def test_calendly_account_forms(monkeypatch, raw) -> None:
    monkeypatch.setenv(checks.CALENDLY_ACCOUNT_ENV, raw)
    assert checks.calendly_account() == "acme_jane-doe"
    assert checks.calendly_ok("https://calendly.com/acme_jane-doe/30min")
    assert not checks.calendly_ok("https://calendly.com/acme/30min")


@pytest.mark.parametrize("raw", ["calendly.com", "https://calendly.com/", "  "])
def test_calendly_account_without_account_is_unset(monkeypatch, raw) -> None:
    monkeypatch.setenv(checks.CALENDLY_ACCOUNT_ENV, raw)
    assert checks.calendly_account() == ""
    assert not checks.calendly_ok("https://calendly.com/calendly.com/x")


@pytest.mark.parametrize(
    ("place", "country"),
    [("London, Vereinigtes Königreich", "GB"), ("Wien, Österreich", "AT")],
)
def test_country_names_survived_the_rename(place, country) -> None:
    # The placeholder rename once turned "Königreich" into nonsense.
    from linkedin_mcp_server.linkedin.ext_events import place_facts

    assert place_facts(place)["country"] == country


def test_state_file_uses_one_prefixed_file_in_place(tmp_path) -> None:
    # Never renamed: an older server may still write to it (split ledger).
    old = tmp_path / "corp-outreach-ledger.jsonl"
    old.write_text('{"a": 1}\n', encoding="utf-8")
    path = outreach.state_file("outreach-ledger.jsonl", tmp_path)
    assert path == old and old.exists()
    assert not (tmp_path / "outreach-ledger.jsonl").exists()


def test_ledger_default_reads_the_prefixed_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(outreach.LEDGER_ENV, raising=False)
    monkeypatch.setattr(outreach.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".linkedin-mcp").mkdir()
    old = tmp_path / ".linkedin-mcp" / "corp-outreach-ledger.jsonl"
    old.write_text("", encoding="utf-8")
    assert outreach.ledger_path() == old


def test_state_file_plain_next_to_prefixed_is_a_split(tmp_path) -> None:
    (tmp_path / "outreach-ledger.jsonl").write_text("new\n", encoding="utf-8")
    (tmp_path / "corp-outreach-ledger.jsonl").write_text("old\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="side by side"):
        outreach.state_file("outreach-ledger.jsonl", tmp_path)


def test_state_file_plain_alone(tmp_path) -> None:
    (tmp_path / "outreach-ledger.jsonl").write_text("new\n", encoding="utf-8")
    assert outreach.state_file("outreach-ledger.jsonl", tmp_path).read_text(
        encoding="utf-8"
    ) == "new\n"


def test_state_file_two_candidates_refuse_a_fresh_ledger(tmp_path) -> None:
    for prefix in ("a", "b"):
        (tmp_path / f"{prefix}-outreach-ledger.jsonl").write_text("x\n")
    with pytest.raises(RuntimeError, match="several old state files"):
        outreach.state_file("outreach-ledger.jsonl", tmp_path)
    assert not (tmp_path / "outreach-ledger.jsonl").exists()


def test_state_file_nothing_to_adopt(tmp_path) -> None:
    assert outreach.state_file("contact-notes.json", tmp_path) == (
        tmp_path / "contact-notes.json"
    )


def test_missing_deployment_values_names_what_is_disabled(monkeypatch) -> None:
    import linkedin_mcp_server.tools.ext as m

    assert m.missing_deployment_values() == []
    monkeypatch.setattr(outreach, "DEFAULT_CANARY", "")
    monkeypatch.delenv(checks.CALENDLY_ACCOUNT_ENV, raising=False)
    missing = m.missing_deployment_values()
    assert [item.split(" ")[0] for item in missing] == [
        outreach.CANARY_ENV,
        checks.CALENDLY_ACCOUNT_ENV,
    ]


def test_register_warns_once_per_missing_value(monkeypatch, caplog) -> None:
    from fastmcp import FastMCP

    import linkedin_mcp_server.tools.ext as m

    monkeypatch.setattr(outreach, "DEFAULT_CANARY", "")
    with caplog.at_level("WARNING", logger=m.__name__):
        m.register_ext_tools(FastMCP("t"))
    hits = [r for r in caplog.records if "not configured" in r.getMessage()]
    assert len(hits) == 1 and outreach.CANARY_ENV in hits[0].getMessage()


def _call(name, args):
    import asyncio

    from fastmcp import Client, FastMCP

    import linkedin_mcp_server.tools.ext as m

    mcp = FastMCP("t")
    m.register_ext_tools(mcp)

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(name, args)).structured_content

    return asyncio.run(go())


def test_unset_canary_refuses_campaign_and_selftest(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(outreach.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(outreach, "DEFAULT_CANARY", "")
    campaign = _call(
        "send_campaign_batch",
        {"message": "Hallo", "recipients": ["a"], "campaign": "c", "confirm_send": True},
    )
    assert campaign["status"] == "canary_not_configured"
    assert _call("outreach_selftest", {})["status"] == "canary_not_configured"
    assert not (tmp_path / "ledger.jsonl").exists()
