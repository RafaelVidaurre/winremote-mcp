"""Real scheduled-task restart with a listener that outlives its initial process."""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows scheduled tasks and socket sharing")
def test_restart_waits_for_original_listener_release_and_preserves_owned_worker(tmp_path):
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
            "-RunRoot",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "restarted"
    assert receipt["listener_pid"] != receipt["previous_pid"]

    from winremote.runs import Runs, alive

    runs = Runs(tmp_path / "runs")
    run_id = (runs.root / "resident-id").read_text()
    try:
        resident = runs.get(run_id)
        assert resident["state"] == "running", resident
        assert alive(resident["worker_pid"], resident["worker_birth"])
        assert alive(resident["child_pid"], resident["child_birth"])
        (runs.root / "resident.release").touch()
        deadline = time.monotonic() + 10
        while runs.get(run_id)["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
        completed = runs.get(run_id)
        assert completed["state"] == "succeeded" and completed["cleanup"] == "complete", completed
    finally:
        runs.cancel(run_id, "test/restart-resident")
