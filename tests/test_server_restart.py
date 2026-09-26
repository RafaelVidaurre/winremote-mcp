"""Real scheduled-task restart with a listener that outlives its initial process."""

import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows scheduled tasks and socket sharing")
def test_restart_waits_for_original_listener_release():
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "tests/fixtures/restart_task.ps1"),
            "-TaskName",
            "WinremoteRestartTest-" + uuid.uuid4().hex,
            "-Python",
            sys.executable,
            "-Server",
            str(root / "tests/fixtures/delayed_listener.py"),
            "-RestartScript",
            str(root / "tools/restart-server-task.ps1"),
            "-Port",
            str(port),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "restarted"
    assert receipt["listener_pid"] != receipt["previous_pid"]
