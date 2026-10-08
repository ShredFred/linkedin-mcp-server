"""Regenerate or check tests/fixtures/tool-contract/tools.json.

Fork extension. The fixture is the wire form of ``tools/list`` without the
fork's own ``ext`` tools, exactly as ``tests/test_server.py`` compares it. A
merge conflict in it is resolved by regenerating it from the merged code
rather than by editing JSON by hand.

    uv run python scripts/ext_tool_contract.py           # write
    uv run python scripts/ext_tool_contract.py --check   # exit 1 on drift
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from _pytest.monkeypatch import MonkeyPatch  # noqa: E402

from tests import test_server  # noqa: E402


def _render() -> str:
    role = next(r for r in test_server.ServerRole if r.drives_browser)
    mp = MonkeyPatch()
    try:
        tools = asyncio.run(
            test_server._served_tool_contract(mp, role, "legacy", "2025-11-25")
        )
    finally:
        mp.undo()
    return json.dumps(tools, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = test_server._TOOL_CONTRACT
    text = _render()
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(f"{target.relative_to(ROOT)} is out of date", file=sys.stderr)
            return 1
        return 0
    target.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
