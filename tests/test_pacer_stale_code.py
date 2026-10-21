"""A server process that outlives a budget change says so (2026-10-18)."""

from linkedin_mcp_server import ext_outreach as o


def test_fresh_module_is_not_stale():
    assert o.code_stale() is False


def test_changed_signature_marks_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(o, "_LOADED_SIGNATURE", (1, 1))
    assert o.code_stale() is True
    s = o.Pacer(o.Ledger(tmp_path / "l.jsonl")).summary()
    assert "stale_code" in s and s["actions"]["search"]["stale_code"] is True
