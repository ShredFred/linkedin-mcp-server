import subprocess
import sys

import pytest

from linkedin_mcp_server import no_console


def test_adds_create_no_window_by_default():
    assert no_console.effective_flags(0) & 0x08000000


def test_keeps_an_explicit_console_choice():
    detached = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
    assert no_console.effective_flags(detached) == detached
    assert no_console.effective_flags(0x00000010) == 0x00000010


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_package_import_patches_popen_once():
    import linkedin_mcp_server  # noqa: F401

    assert no_console.install() is True
    assert getattr(subprocess.Popen.__init__, no_console._MARK, False)
    done = subprocess.run(
        [sys.executable, "-c", "print(1)"], capture_output=True, check=True
    )
    assert done.stdout.strip() == b"1"
