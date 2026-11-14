"""Real worker/child custody with a controlled queue read outage."""

import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psutil
import pytest

from winremote.run_worker import execute
from winremote.runs import Runs


@pytest.mark.parametrize("after_outage", ["complete", "cancel", "timeout"])
def test_queue_read_outage_preserves_child_and_still_honors_lifecycle(tmp_path, monkeypatch, after_outage):
    runs = Runs(tmp_path / "queue")
    started, release = tmp_path / "started", tmp_path / "release"
    code = (
        f"from pathlib import Path; import os,time; Path({str(started)!r}).write_text(str(os.getpid()))\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.02)"
    )
    run = runs.submit(
        [sys.executable, "-c", code],
        str(tmp_path),
        "test/metadata",
        timeout=2 if after_outage == "timeout" else 10,
        spawn=False,
    )
    original = Runs.get
    restored = threading.Event()
    attempts = []
    main_thread = threading.get_ident()

    def get(self, run_id):
        if threading.get_ident() != main_thread and started.exists() and not restored.is_set():
            attempts.append(time.monotonic())
            error = sqlite3.OperationalError("disk I/O error")
            error.sqlite_errorcode = sqlite3.SQLITE_IOERR
            error.sqlite_errorname = "SQLITE_IOERR"
            raise error
        return original(self, run_id)

    monkeypatch.setattr(Runs, "get", get)
    with ThreadPoolExecutor(1) as pool:
        worker = pool.submit(execute, runs.root, run["id"])
        try:
            deadline = time.monotonic() + 5
            while len(attempts) < 3 and not worker.done() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(attempts) >= 3, "metadata error terminated the worker instead of retrying"
            assert psutil.pid_exists(int(started.read_text())), "queue read failure killed the owned child"
            if after_outage == "complete":
                restored.set()
                release.touch()
            elif after_outage == "cancel":
                runs.cancel(run["id"], run["owner"])
                restored.set()
            worker.result(timeout=5)
            result = runs.get(run["id"])
            expected = {"complete": "succeeded", "cancel": "cancelled", "timeout": "timeout"}
            assert result["state"] == expected[after_outage]
            assert result["cleanup"] == "complete"
        finally:
            restored.set()
            release.touch()


def test_restart_refuses_legacy_workers_and_exempts_only_its_own_ancestor(tmp_path):
    from winremote.maintenance import restart_blockers

    runs = Runs(tmp_path / "queue")
    legacy = runs.submit(["unused"], str(tmp_path), "test/legacy", spawn=False)
    own = runs.submit(["unused"], str(tmp_path), "test/maintenance", spawn=False)
    runs.update(own["id"], worker_pid=42)
    assert [r["id"] for r in restart_blockers(runs, {42})] == [legacy["id"]]
    runs.update(legacy["id"], metadata_outage_safe=True)
    assert restart_blockers(runs, {42}) == []
    assert [r["id"] for r in restart_blockers(runs, set())] == [own["id"]]
