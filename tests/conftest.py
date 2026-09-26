"""Shared fixtures for winremote-mcp tests.

Mocks win32 and display-dependent modules so tests run on headless Linux.
"""

from __future__ import annotations

import mimetypes
import os
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Must happen before ANY winremote import
# ---------------------------------------------------------------------------

os.environ.setdefault("DISPLAY", ":0")


# Python 3.12 mimetypes checks for winreg importability, so our mocked winreg
# can accidentally trigger Windows-registry MIME loading on Linux. Neutralize
# that code path before importing winremote/fastmcp.
def _skip_windows_registry(*args, **kwargs):
    return None


mimetypes.read_windows_registry = _skip_windows_registry
mimetypes.MimeTypes.read_windows_registry = _skip_windows_registry
mimetypes.MimeTypes._read_windows_registry = _skip_windows_registry

# Mock all problematic native modules
_mock_modules = [
    "Xlib",
    "Xlib.display",
    "Xlib.xauth",
    "Xlib.error",
    "Xlib.protocol",
    "Xlib.protocol.display",
    "Xlib.protocol.rq",
    "Xlib.support",
    "Xlib.support.connect",
    "Xlib.support.unix_connect",
    "Xlib.ext",
    "Xlib.ext.xtest",
    "Xlib.X",
    "Xlib.XK",
    "Xlib.keysymdef",
    "Xlib.keysymdef.latin1",
    "mouseinfo",
    "win32api",
    "win32gui",
    "win32con",
    "win32process",
    "win32clipboard",
    "winreg",
]

for mod_name in _mock_modules:
    if mod_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        m.__file__ = f"<mock {mod_name}>"
        m.__spec__ = None
        sys.modules[mod_name] = m

# Mock pyautogui on non-Windows (no X11/display available)
try:
    import pyautogui  # noqa: E402

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
except Exception:
    # Create a full mock pyautogui so test_desktop_tools.py can at least import
    import types

    _mock_pyautogui = types.ModuleType("pyautogui")
    for _attr in [
        "click",
        "doubleClick",
        "moveTo",
        "drag",
        "scroll",
        "hscroll",
        "hotkey",
        "press",
        "typewrite",
        "write",
        "position",
        "screenshot",
        "keyDown",
        "keyUp",
        "FAILSAFE",
        "PAUSE",
    ]:
        setattr(_mock_pyautogui, _attr, MagicMock())
    sys.modules["pyautogui"] = _mock_pyautogui
    globals()["pyautogui"] = _mock_pyautogui

# Now import pyautogui safely (graceful fallback on headless Linux)
try:
    import pyautogui  # noqa: E402

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
except Exception:
    pyautogui = None  # type: ignore

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_pyautogui(monkeypatch):
    """Prevent any real mouse/keyboard actions during tests."""
    if pyautogui is not None:
        monkeypatch.setattr(pyautogui, "click", MagicMock())
        monkeypatch.setattr(pyautogui, "doubleClick", MagicMock())
        monkeypatch.setattr(pyautogui, "moveTo", MagicMock())
        monkeypatch.setattr(pyautogui, "drag", MagicMock())
        monkeypatch.setattr(pyautogui, "scroll", MagicMock())
        monkeypatch.setattr(pyautogui, "hscroll", MagicMock())
        monkeypatch.setattr(pyautogui, "hotkey", MagicMock())
        monkeypatch.setattr(pyautogui, "press", MagicMock())
        monkeypatch.setattr(pyautogui, "typewrite", MagicMock())
        monkeypatch.setattr(pyautogui, "write", MagicMock())
        monkeypatch.setattr(pyautogui, "position", MagicMock(return_value=(500, 500)))


@pytest.fixture(autouse=True)
def _isolated_run_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WINREMOTE_RUNS_DIR", str(tmp_path / "winremote-state"))
