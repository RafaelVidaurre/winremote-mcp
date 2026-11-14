"""Real SQLite contention and worker publication; no access to the host queue."""

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import psutil

from winremote.runs import Runs


def queued(runs, path, **kwargs):
    run = runs.submit(["unused-test-command"], str(path), "test/contention", spawn=False, **kwargs)
    runs.update(run["id"], worker_pid=os.getpid(), worker_birth=psutil.Process().create_time())
    return run


def test_status_and_lists_do_not_wait_for_an_unrelated_writer(tmp_path):
    runs = Runs(tmp_path / "state")
    run = queued(runs, tmp_path)
    writer = sqlite3.connect(runs.db)
    writer.execute("BEGIN IMMEDIATE")
    pool = ThreadPoolExecutor(1)
    try:

        def inspect():
            client = Runs(runs.root)
            client.recover()  # RunStatus does this before get().
            return client.get(run["id"]), client.list(), client.list(False)

        future = pool.submit(inspect)
        status, active, history = future.result(timeout=2)
        assert status["state"] == "queued"
        assert active["runs"][0]["id"] == history["runs"][0]["id"] == run["id"]
    finally:
        writer.rollback()
        writer.close()
        pool.shutdown(wait=True)


def test_blocked_admission_does_not_join_the_writer_queue(tmp_path):
    runs = Runs(tmp_path / "state")
    holder = queued(runs, tmp_path)
    runs.update(holder["id"], state="running")
    waiter = queued(runs, tmp_path)
    writer = sqlite3.connect(runs.db)
    writer.execute("BEGIN IMMEDIATE")
    pool = ThreadPoolExecutor(1)
    try:
        assert pool.submit(runs.admit, waiter["id"]).result(timeout=2) is False
    finally:
        writer.rollback()
        writer.close()
        pool.shutdown(wait=True)


def test_recovery_does_not_hold_writer_lock_while_probing_or_overwrite_new_state(tmp_path):
    runs = Runs(tmp_path / "state")
    run = queued(runs, tmp_path)
    entered, release = threading.Event(), threading.Event()

    def probe(*_):
        entered.set()
        assert release.wait(3)
        return False

    pool = ThreadPoolExecutor(2)
    try:
        with mock.patch("winremote.runs.alive", side_effect=probe):
            recovery = pool.submit(runs.recover)
            assert entered.wait(2)
            # A completion racing the external liveness probe must win over that snapshot.
            completion = pool.submit(runs.update, run["id"], state="succeeded", cleanup="complete")
            completion.result(timeout=2)
            release.set()
            recovery.result(timeout=2)
        assert runs.get(run["id"])["state"] == "succeeded"
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_history_limit_is_applied_before_json_decoding_and_active_history_is_indexed(tmp_path):
    runs = Runs(tmp_path / "state")
    run = queued(runs, tmp_path)
    with runs.connect() as db:
        for n in range(1000):
            row = dict(run, id=f"old-{n}", state="succeeded", command=["x" * 4096])
            db.execute("INSERT INTO runs(id,data) VALUES (?,?)", (row["id"], json.dumps(row)))
    original = json.loads
    decoded = []

    def decode(value, *args, **kwargs):
        decoded.append(value)
        return original(value, *args, **kwargs)

    with mock.patch("winremote.runs.json.loads", side_effect=decode):
        history = runs.list(False, limit=3)["runs"]
    assert [r["id"] for r in history] == ["old-997", "old-998", "old-999"]
    assert len(decoded) < 10  # Active recovery + requested history, not all terminal payloads.
    with runs.connect(write=False) as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT data FROM runs WHERE "
            "json_extract(data, '$.state') IN ('queued','running','cleanup_uncertain')"
        ).fetchall()
    assert any("SEARCH" in row[3] for row in plan), plan


def test_parent_publishes_worker_identity_before_slow_child_registration(tmp_path):
    runs = Runs(tmp_path / "state")
    # Process creation succeeds but the child has not yet imported/registered.
    with mock.patch("winremote.runs.subprocess.Popen", return_value=mock.Mock(pid=os.getpid())):
        run = runs.submit(["unused-test-command"], str(tmp_path), "test/startup")
    runs.update(run["id"], created_at=time.time() - 40)
    runs.recover()
    observed = runs.get(run["id"])
    assert observed["worker_pid"] == os.getpid()
    assert observed["state"] == "queued"


def test_active_custody_is_complete_even_when_history_limit_is_small(tmp_path):
    runs = Runs(tmp_path / "state")
    for n in range(4):
        queued(runs, tmp_path, request_id=f"resident-{n}")
    assert len(runs.list(active_only=True, limit=1)["runs"]) == 4


def test_dead_worker_recovery_retains_claims_when_another_writer_is_busy(tmp_path):
    runs = Runs(tmp_path / "state")
    run = queued(runs, tmp_path)
    writer = sqlite3.connect(runs.db)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with mock.patch("winremote.runs.alive", return_value=False):
            runs.recover()
        assert runs.get(run["id"])["state"] == "queued"
    finally:
        writer.rollback()
        writer.close()
    with mock.patch("winremote.runs.alive", return_value=False):
        runs.recover()
    assert runs.get(run["id"])["state"] == "interrupted"


def test_failed_parent_bookkeeping_does_not_release_a_spawned_worker(tmp_path):
    runs = Runs(tmp_path / "state")
    with mock.patch("winremote.runs.subprocess.Popen", return_value=mock.Mock(pid=os.getpid())):
        with mock.patch("winremote.runs.psutil.Process", side_effect=RuntimeError("identity unavailable")):
            import pytest

            with pytest.raises(RuntimeError, match="identity unavailable"):
                runs.submit(["unused-test-command"], str(tmp_path), "test/spawned")
    retained = runs.list()["runs"]
    assert len(retained) == 1
    assert retained[0]["state"] == "queued" and retained[0]["cleanup"] == "pending"
