"""Behavioral evidence for durable admission, isolation, and owned process cleanup."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from fastmcp import Client, FastMCP

from winremote.artifacts import Artifacts, digest
from winremote.client import download, make_bundle, upload
from winremote.run_tools import register
from winremote.runs import Runs, alive, path_resource
from winremote.source_stage import stage


def until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("condition did not become true within deadline")


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("WINREMOTE_RUNS_DIR", str(tmp_path / "state"))
    value = Runs()
    yield value
    for r in value.list()["runs"]:
        if r["kind"] == "run":
            value.cancel(r["id"], r["owner"])
    until(lambda: not [r for r in value.list()["runs"] if r["kind"] == "run"])


def held(runs, tmp_path, label, **kwargs):
    start, release = tmp_path / (label + ".started"), tmp_path / (label + ".release")
    code = (
        f"from pathlib import Path; import time; Path({str(start)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.05)"
    )
    run = runs.submit(
        [sys.executable, "-c", code], str(tmp_path), "test/" + label, workspace_access="shared", timeout=20, **kwargs
    )
    return run, start, release


def finished(runs, run):
    return until(lambda: r if (r := runs.get(run["id"]))["state"] not in ("queued", "running") else None)


def test_independent_runs_overlap_and_survive_client_recreation(runs, tmp_path):
    a, am, ar = held(runs, tmp_path, "a")
    b, bm, br = held(runs, tmp_path, "b")
    until(lambda: am.exists() and bm.exists())
    assert Runs(runs.root).get(a["id"])["state"] == "running"
    ar.touch()
    br.touch()
    assert finished(runs, a)["cleanup"] == finished(runs, b)["cleanup"] == "complete"


def test_exclusive_waits_for_drain_then_blocks_later_shared_work(runs, tmp_path):
    a, am, ar = held(runs, tmp_path, "a")
    until(am.exists)
    exclusive, em, er = held(runs, tmp_path, "exclusive", isolation="exclusive")
    b, bm, br = held(runs, tmp_path, "b")
    assert not runs.admit(exclusive["id"]) and not runs.admit(b["id"])
    assert not em.exists() and not bm.exists()
    with pytest.raises(RuntimeError, match="host_busy"):
        with runs.operation("FileWrite"):
            pytest.fail("a direct tool bypassed the exclusive barrier")
    ar.touch()
    until(em.exists)
    assert not bm.exists()
    er.touch()
    until(bm.exists)
    br.touch()
    assert finished(runs, exclusive)["state"] == "succeeded"


def test_disjoint_resource_bypasses_an_older_blocked_request(runs, tmp_path):
    a, am, ar = held(runs, tmp_path, "a", resources={"port:tcp:4000": "exclusive"})
    until(am.exists)
    b, bm, br = held(runs, tmp_path, "b", resources={"port:tcp:4000": "exclusive"})
    c, cm, cr = held(runs, tmp_path, "c", resources={"port:tcp:5000": "exclusive"})
    until(cm.exists)
    assert not bm.exists()
    ar.touch()
    cr.touch()
    until(bm.exists)
    br.touch()
    assert finished(runs, b)["state"] == "succeeded"


def test_parent_path_write_conflicts_with_child_path_read(runs, tmp_path):
    from winremote.runs import resources_conflict

    assert resources_conflict({path_resource(tmp_path.anchor): "exclusive"}, {path_resource(str(tmp_path)): "shared"})
    parent = tmp_path / "data"
    parent.mkdir()
    child = parent / "nested"
    child.mkdir()
    with runs.operation("writer", {path_resource(str(parent)): "exclusive"}):
        with pytest.raises(RuntimeError, match="host_busy"):
            with runs.operation("reader", {path_resource(str(child)): "shared"}):
                pytest.fail("overlapping paths admitted")
    with runs.operation("reader", {path_resource(str(parent)): "shared"}):
        with runs.operation("reader", {path_resource(str(child)): "shared"}):
            pass


def test_cancel_kills_owned_descendant_and_preserves_peer(runs, tmp_path):
    pidfile = tmp_path / "child.json"
    code = (
        "import subprocess,sys,json,time,psutil; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(pidfile)!r}).write_text(json.dumps([p.pid,psutil.Process(p.pid).create_time()])); time.sleep(60)"
    )
    a = runs.submit([sys.executable, "-c", code], str(tmp_path), "test/a", workspace_access="shared", timeout=20)
    b, bm, br = held(runs, tmp_path, "b")
    until(lambda: pidfile.exists() and bm.exists())
    pid, birth = json.loads(pidfile.read_text())
    with pytest.raises(ValueError, match="owner"):
        runs.cancel(a["id"], "someone-else")
    runs.cancel(a["id"], "test/a")
    result = finished(runs, a)
    assert result["state"] == "cancelled" and result["cleanup"] == "complete"
    assert not alive(pid, birth)
    assert runs.get(b["id"])["state"] == "running"
    br.touch()
    assert finished(runs, b)["state"] == "succeeded"


def test_execution_deadline_and_queued_cancellation(runs, tmp_path):
    a = runs.submit([sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path), "test/a", timeout=0.3)
    b = runs.submit([sys.executable, "-c", "raise SystemExit(99)"], str(tmp_path), "test/b")
    runs.cancel(b["id"], "test/b")
    assert finished(runs, b)["state"] == "cancelled"
    result = finished(runs, a)
    assert result["state"] == "timeout" and result["cleanup"] == "complete"


def test_repeated_submission_is_one_run_and_changed_payload_is_rejected(runs, tmp_path):
    args = dict(command=[sys.executable, "-c", 'print("hello")'], cwd=str(tmp_path), owner="test/a", request_id="once")
    first = runs.submit(**args)
    assert runs.submit(**args)["id"] == first["id"]
    with pytest.raises(ValueError, match="different"):
        runs.submit(**dict(args, command=[sys.executable, "-c", 'print("different")']))
    assert finished(runs, first)["state"] == "succeeded"
    assert runs.submit(**args)["id"] == first["id"]


def test_nonzero_exit_is_a_failure_with_complete_logs(runs, tmp_path):
    a = runs.submit([sys.executable, "-c", 'print("meaningful failure"); raise SystemExit(7)'], str(tmp_path), "test/a")
    result = finished(runs, a)
    assert result["state"] == "failed" and result["exit_code"] == 7 and result["cleanup"] == "complete"
    assert "meaningful failure" in (Path(result["output_dir"]) / "output.log").read_text()


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows kernel Job Object behavior")
def test_worker_crash_closes_job_and_releases_only_after_recovery(runs, tmp_path):
    a, started, _ = held(runs, tmp_path, "crash")
    until(started.exists)
    state = runs.get(a["id"])
    psutil.Process(state["worker_pid"]).kill()
    until(lambda: not alive(state["worker_pid"], state["worker_birth"]))
    until(lambda: (runs.recover() or runs.get(a["id"])["state"]) == "interrupted")
    assert runs.get(a["id"])["cleanup"] == "complete"


def test_artifact_resume_rejects_corruption_and_never_overwrites(runs, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(os.urandom(700000))
    target = tmp_path / "target"
    api = Artifacts(runs)
    info = api.begin(str(target), source.stat().st_size, digest(source))
    first = source.read_bytes()[:12345]
    api.chunk(info["transfer_id"], 0, base64.b64encode(first).decode())
    assert api.begin(str(target), source.stat().st_size, digest(source))["offset"] == len(first)
    with pytest.raises(Exception):
        api.chunk(info["transfer_id"], len(first), "invalid base64!")
    assert api.begin(str(target), source.stat().st_size, digest(source))["offset"] == len(first)
    with pytest.raises(ValueError, match="mismatch"):
        api.finish(info["transfer_id"])
    target.write_bytes(b"other owner")
    with pytest.raises(ValueError, match="destination"):
        api.finish(info["transfer_id"])
    assert target.read_bytes() == b"other owner"


def test_binary_roundtrip_through_real_mcp_serialization(runs, tmp_path):
    server = FastMCP("artifact-test")
    register(server)
    source = tmp_path / "source.bin"
    source.write_bytes(os.urandom(1200000))
    remote = tmp_path / "remote.bin"
    restored = tmp_path / "restored.bin"

    async def exercise():
        async with Client(server) as client:
            sent = await upload(client, source, str(remote))
            assert sent["sha256"] == digest(source)
            got = await download(client, str(remote), restored)
            assert got["sha256"] == digest(source)

    asyncio.run(exercise())
    assert restored.read_bytes() == source.read_bytes()


def test_stage_bundle_creates_exact_new_checkout_and_preserves_existing(runs, tmp_path):
    repo = tmp_path / "source-repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT, text=True).strip()

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "unicode.txt").write_text("✕ test input", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "fixture")
    bundle = tmp_path / "source.bundle"
    commit, ref = make_bundle(str(repo), "HEAD", None, bundle)
    cache = tmp_path / "cache.git"
    dest = tmp_path / "checkout"
    result = stage(str(bundle), str(cache), str(dest), commit, ref)
    assert result["commit"] == commit
    assert (dest / "unicode.txt").read_text(encoding="utf-8") == "✕ test input"
    with pytest.raises(ValueError, match="new path"):
        stage(str(bundle), str(cache), str(dest), commit, ref)
