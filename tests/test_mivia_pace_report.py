"""pace_report: the read-only view HQ plans its search share from."""

from linkedin_mcp_server import mivia_outreach as outreach


def test_pace_report_reads_without_booking(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv(outreach.LEDGER_ENV, str(ledger))
    outreach.Pacer(outreach.Ledger.default()).take("search", 5, tool="t")
    before = ledger.read_text(encoding="utf-8")
    rep = outreach.pace_report()
    assert rep["schema"] == "mivia-pace-report.v1"
    s = rep["actions"]["search"]
    assert s["today"] == 5 and s["per_day"] == outreach.PACE_BUDGETS["search"]["day"]
    assert s["left"] == outreach.PACE_BUDGETS["search"]["day"] - 5
    assert rep["write_total_per_day"] == outreach.PACE_WRITE_TOTAL_PER_DAY
    assert ledger.read_text(encoding="utf-8") == before
