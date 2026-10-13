"""Hold a target behind a parent-controlled release frame."""

from __future__ import annotations

import os
import subprocess
import sys

_PREFIX = b"release "
_NONCE_BYTES = 32
_NONCE_LENGTH = _NONCE_BYTES * 2
_MAX_FRAME = len(_PREFIX) + _NONCE_LENGTH + 1
# The gate is started detached, so it has no console to share. A console
# target started from it without this flag gets a new, visible console window
# (seen with the daemon owner on Windows). The gate runs with -I -S, so the
# package-wide no_console default is not installed here and must be explicit.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def _arguments(argv: list[str]) -> tuple[str, list[str]]:
    if len(argv) < 4 or argv[2] != "--":
        raise ValueError("The process gate was started without a target command")
    nonce = argv[1]
    try:
        decoded = bytes.fromhex(nonce)
    except ValueError as exc:
        raise ValueError("The process gate received an invalid release nonce") from exc
    if len(nonce) != _NONCE_LENGTH or len(decoded) != _NONCE_BYTES:
        raise ValueError("The process gate received an invalid release nonce")
    return nonce, argv[3:]


def _await_release(nonce: str) -> bool:
    """Read one bounded frame without consuming target stdin behind it."""
    expected = _PREFIX + nonce.encode("ascii") + b"\n"
    frame = bytearray()
    while len(frame) < _MAX_FRAME:
        piece = os.read(0, 1)
        if not piece:
            return False
        frame.extend(piece)
        if piece == b"\n":
            return bytes(frame) == expected
    return False


def main(argv: list[str] | None = None) -> int:
    """Release the exact target command and mirror its exit status."""
    nonce, command = _arguments(sys.argv if argv is None else argv)
    if not _await_release(nonce):
        return 70
    try:
        target = subprocess.Popen(
            command,
            stdin=0,
            stdout=1,
            stderr=2,
            close_fds=True,
            creationflags=_NO_WINDOW,
        )
    except OSError:
        return 70
    return target.wait()


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
