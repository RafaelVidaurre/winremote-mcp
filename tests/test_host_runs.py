"""Behavioral evidence for durable admission, isolation, and owned process cleanup."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

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


REAL_QUEUE = (Path.home() / ".local/share/winremote/runs").resolve()


@pytest.fixture(autouse=True)
def fence_real_queue():
    """Abort any default-root queue access, independently of each test's monkeypatch.

    A mock patcher is not reverted by monkeypatch.undo(), so a fixture mistake cannot
    reach the developer's real machine queue.
    """
    import winremote.runs as runs_module

    unfenced = runs_module.state_root

    def fenced():
        root = unfenced()
        if root == REAL_QUEUE or REAL_QUEUE in root.parents or root in REAL_QUEUE.parents:
            raise RuntimeError(f"test fence: refused real winremote queue {root}")
        return root

    with mock.patch.object(runs_module, "state_root", fenced):
        yield


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


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows interactive desktop")
def test_hidden_run_does_not_open_a_worker_console(runs, tmp_path):
    """Observe the real worker from before submission through command startup."""
    import ctypes
    import ctypes.wintypes as wintypes
    import threading

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    stop = threading.Event()
    observed = set()

    @enum_proc
    def inspect_window(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, len(name))
            if name.value in ("PseudoConsoleWindow", "ConsoleWindowClass"):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    if os.getpid() in (p.pid for p in psutil.Process(pid.value).parents()):
                        observed.add((name.value, pid.value))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return True

    def observe():
        while not stop.is_set():
            user32.EnumWindows(inspect_window, 0)
            stop.wait(0.005)

    observer = threading.Thread(target=observe)
    observer.start()
    try:
        run, started, release = held(runs, tmp_path, "hidden-console", hidden=True)
        until(started.exists)
        release.touch()
        result = finished(runs, run)
    finally:
        stop.set()
        observer.join()
    assert result["state"] == "succeeded" and result["cleanup"] == "complete"
    assert not observed, f"hidden run opened console windows: {observed}"


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


# --- Run-scoped observation of an exact held run --------------------------------


def test_observer_token_is_returned_once_and_never_persisted_or_listed(runs, tmp_path):
    a, am, ar = held(runs, tmp_path, "a", request_id="observe-once")
    token = a["observer_token"]
    assert token
    retry = runs.submit(
        [sys.executable, "-c", a["command"][2]],
        str(tmp_path),
        "test/a",
        workspace_access="shared",
        timeout=20,
        request_id="observe-once",
    )
    assert retry["id"] == a["id"] and "observer_token" not in retry
    assert token not in json.dumps(runs.get(a["id"])) and token not in json.dumps(runs.list(False))
    assert token not in (runs.root / "runs.sqlite3").read_bytes().decode("latin-1")
    ar.touch()
    finished(runs, a)


def test_held_run_owner_observes_its_desktop_while_plain_and_forged_calls_are_refused(runs, tmp_path):
    a, am, ar = held(runs, tmp_path, "a", resources={"desktop": "exclusive"})
    b, bm, br = held(runs, tmp_path, "b", resources={"port:tcp:4100": "exclusive"})
    until(lambda: am.exists() and bm.exists())
    with pytest.raises(RuntimeError, match="host_busy"):
        with runs.operation("Snapshot", {"desktop": "exclusive"}):
            pytest.fail("a plain desktop observation bypassed the held run")
    with runs.operation("RunSnapshot", {"desktop": "exclusive"}, within=(a["id"], a["observer_token"])) as owner:
        assert owner["id"] == a["id"]
    for forged in ((a["id"], b["observer_token"]), (a["id"], "x" * 43), (b["id"], a["observer_token"])):
        with pytest.raises(PermissionError, match="observation_denied"):
            with runs.operation("RunSnapshot", {"desktop": "exclusive"}, within=forged):
                pytest.fail("observation admitted without the run's own capability")
    ar.touch()
    finished(runs, a)
    with pytest.raises(PermissionError, match="claims are not held"):
        with runs.operation("RunSnapshot", {"desktop": "exclusive"}, within=(a["id"], a["observer_token"])):
            pytest.fail("a released run was observed")
    br.touch()
    finished(runs, b)


def test_observation_is_limited_to_owned_claims_and_respects_other_owners(runs, tmp_path, tmp_path_factory):
    own, other = tmp_path_factory.mktemp("own"), tmp_path_factory.mktemp("other")
    a, am, ar = held(runs, tmp_path, "a", resources={"path:" + str(own): "exclusive", "desktop": "shared"})
    b, bm, br = held(runs, tmp_path, "b", resources={"path:" + str(other): "exclusive", "desktop": "shared"})
    until(lambda: am.exists() and bm.exists())
    within = (a["id"], a["observer_token"])
    with runs.operation("RunLog", {path_resource(str(own / "godot.log")): "shared"}, within=within):
        pass
    with runs.operation("RunLog", {path_resource(str(Path(a["output_dir"]) / "x.log")): "shared"}, within=within):
        pass
    for claims in ({path_resource(str(other / "godot.log")): "shared"}, {"port:tcp:4200": "shared"}):
        with pytest.raises(PermissionError, match="outside the run's claims"):
            with runs.operation("RunLog", claims, within=within):
                pytest.fail("observation escaped the run's own claims")
    # Both owners share the desktop; an exclusive capture must still respect the other owner.
    with pytest.raises(RuntimeError, match="host_busy"):
        with runs.operation("RunSnapshot", {"desktop": "exclusive"}, within=within):
            pytest.fail("observation overrode another owner's held claim")
    ar.touch()
    br.touch()
    finished(runs, a)
    finished(runs, b)


def test_exclusive_run_owner_observes_without_reordering_the_queue(runs, tmp_path):
    exclusive, em, er = held(runs, tmp_path, "exclusive", isolation="exclusive", resources={"desktop": "exclusive"})
    until(em.exists)
    later, lm, lr = held(runs, tmp_path, "later", resources={"desktop": "exclusive"})
    with pytest.raises(RuntimeError, match="host_busy"):
        with runs.operation("Snapshot", {"desktop": "exclusive"}):
            pytest.fail("a plain call crossed the exclusive barrier")
    with runs.operation("RunSnapshot", {"desktop": "exclusive"}, within=(exclusive["id"], exclusive["observer_token"])):
        assert not runs.admit(later["id"])
    assert runs.get(later["id"])["state"] == "queued" and not lm.exists()
    er.touch()
    until(lm.exists)
    lr.touch()
    assert finished(runs, later)["state"] == "succeeded"


def test_mcp_run_snapshot_and_live_log_tail_record_nonsecret_receipts(runs, tmp_path, monkeypatch):
    import hashlib

    from winremote import desktop
    from winremote.client import keep_observer_token, reserve_observer_token_file, snapshot

    jpeg = b"\xff\xd8fixture-jpeg\xff\xd9"
    window = type("W", (), dict(handle=7, title="Pocket Space Agency", width=2560, height=1440, rect=(0, 0, 0, 0)))
    monkeypatch.setattr(desktop, "take_screenshot", lambda **_: base64.b64encode(jpeg).decode())
    monkeypatch.setattr(desktop, "enumerate_windows", lambda: [window])
    data = tmp_path / "userdata"
    data.mkdir()
    log = data / "godot.log"
    log.write_bytes(b"boot\n")
    a, am, ar = held(runs, tmp_path, "a", resources={"desktop": "exclusive", "path:" + str(data): "exclusive"})
    until(am.exists)
    token = a["observer_token"]
    server = FastMCP("observe-test")
    register(server)

    async def exercise():
        async with Client(server) as client:
            got = await snapshot(client, a["id"], token, tmp_path / "snap.jpg")
            assert (tmp_path / "snap.jpg").read_bytes() == jpeg and got["windows"][0]["title"] == "Pocket Space Agency"
            first = (await client.call_tool("RunLog", dict(run_id=a["id"], path=str(log), observer_token=token))).data
            log.write_bytes(b"boot\nmain scene ready\n")
            second = (
                await client.call_tool(
                    "RunLog", dict(run_id=a["id"], path=str(log), offset=first["next_offset"], observer_token=token)
                )
            ).data
            assert first["text"] == "boot\n" and second["text"] == "main scene ready\n"
            denied = await client.call_tool(
                "RunLog", dict(run_id=a["id"], path=str(log), observer_token="wrong"), raise_on_error=False
            )
            assert denied.is_error

    asyncio.run(exercise())
    receipts = (Path(a["output_dir"]) / "observations.jsonl").read_text(encoding="utf-8")
    assert token not in receipts
    rows = [json.loads(line) for line in receipts.splitlines()]
    assert [r["tool"] for r in rows] == ["RunSnapshot", "RunLog", "RunLog"]
    assert rows[0]["sha256"] == hashlib.sha256(jpeg).hexdigest() and rows[0]["machine_id"] == runs.machine["id"]
    printed = keep_observer_token(dict(id=a["id"], observer_token=token))
    assert token not in json.dumps(printed)
    stored = keep_observer_token(dict(observer_token=token), reserve_observer_token_file(str(tmp_path / "token")))
    assert token not in json.dumps(stored) and (tmp_path / "token").read_text() == token
    if os.name != "nt":
        assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600
    ar.touch()
    finished(runs, a)


def test_run_snapshot_is_a_registered_tier3_tool_with_a_required_capability_schema():
    from winremote.__main__ import _get_registered_tools
    from winremote.tiers import TOOL_TIERS, resolve_enabled_tools

    assert "RunSnapshot" in TOOL_TIERS["tier3"]
    assert "RunSnapshot" in resolve_enabled_tools(enable_tier3=True)
    assert "RunSnapshot" not in resolve_enabled_tools()
    tool = _get_registered_tools()["RunSnapshot"]
    schema = tool.parameters
    assert {"run_id", "observer_token"} <= set(schema["required"])
    assert not {"x", "y", "text", "keys"} & set(schema["properties"])
    log = _get_registered_tools()["RunLog"].parameters
    assert {"path", "observer_token"} <= set(log["properties"]) and log["required"] == ["run_id"]


def _local_submit_args(tmp_path, token_out, request_id):
    import argparse

    spec = tmp_path / (request_id + ".json")
    spec.write_text(
        json.dumps(
            dict(
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=str(tmp_path),
                owner="test/custody",
                request_id=request_id,
                workspace_access="shared",
                timeout=20,
            )
        )
    )
    return argparse.Namespace(action="submit", spec=str(spec), observer_token_out=str(token_out))


def test_token_destination_is_refused_before_admission(runs, tmp_path):
    from winremote.client import local_main

    existing = tmp_path / "existing.token"
    existing.write_text("someone else's file")
    with pytest.raises(FileExistsError):
        local_main(_local_submit_args(tmp_path, existing, "refused-before"))
    assert existing.read_text() == "someone else's file"
    assert not [r for r in runs.list(False)["runs"] if r.get("request_id") == "refused-before"]


def test_fixture_mistake_cannot_reach_the_real_queue(runs, monkeypatch):
    monkeypatch.undo()  # the earlier mistake: reverts the isolated WINREMOTE_RUNS_DIR
    with pytest.raises(RuntimeError, match="test fence"):
        Runs()
    with pytest.raises(RuntimeError, match="test fence"):
        Runs().submit([sys.executable, "-c", "pass"], str(Path.home()), "test/fence")


def _reserved_failure(tmp_path, name):
    from winremote.client import reserve_observer_token_file

    return reserve_observer_token_file(str(tmp_path / name)), dict(
        id="run-under-test", state="running", observer_token="secret-capability"
    )


def test_partial_token_write_closes_before_removal_and_never_loops(tmp_path, monkeypatch):
    import winremote.client as client

    reservation, result = _reserved_failure(tmp_path, "partial.token")
    real_write, real_close, real_unlink = os.write, os.close, Path.unlink
    events, writes = [], iter([3, 0])

    def partial_write(fd, data):
        return real_write(fd, data[: next(writes)])

    def recorded_close(fd):
        events.append("close")
        real_close(fd)

    def recorded_unlink(path, *args, **kwargs):
        events.append("unlink")
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(client.os, "write", partial_write)
        scoped.setattr(client.os, "close", recorded_close)
        scoped.setattr(Path, "unlink", recorded_unlink)
        kept = client.keep_observer_token(result, reservation)
    assert events == ["close", "unlink"]
    assert not (tmp_path / "partial.token").exists()
    assert kept["id"] == "run-under-test" and kept["observer_token_persistence"] == "failed"
    assert kept["observer_token_cleanup"] == "removed" and "secret-capability" not in json.dumps(kept)


def test_close_and_removal_failures_keep_the_admitted_receipt(tmp_path, monkeypatch):
    import winremote.client as client

    reservation, result = _reserved_failure(tmp_path, "stuck.token")
    real_close = os.close

    def failing_close(fd):
        real_close(fd)
        raise OSError(5, "I/O error")

    def failing_unlink(path, *args, **kwargs):
        raise PermissionError(13, "in use")

    with monkeypatch.context() as scoped:
        scoped.setattr(client.os, "close", failing_close)
        scoped.setattr(Path, "unlink", failing_unlink)
        kept = client.keep_observer_token(result, reservation)
    target = tmp_path / "stuck.token"
    assert kept["id"] == "run-under-test" and kept["state"] == "running"
    assert kept["observer_token_persistence"] == "failed"
    assert kept["observer_token_cleanup"].startswith("failed: PermissionError removing")
    assert str(target) in kept["observer_token_cleanup"] and "secret-capability" not in json.dumps(kept)
    target.unlink()


def test_admitted_run_receipt_survives_token_persistence_failure_without_duplication(runs, tmp_path, monkeypatch):
    import winremote.client as client

    target = tmp_path / "lost.token"

    def failing_write(fd, data):
        raise OSError(28, "No space left on device")

    # Scope the failure to this one write; the fixture's isolated queue must stay in effect.
    with monkeypatch.context() as scoped:
        scoped.setattr(client.os, "write", failing_write)
        result = client.local_main(_local_submit_args(tmp_path, target, "persist-fails"))
    assert Runs().root == runs.root
    assert result["observer_token_persistence"] == "failed" and result["state"] in ("queued", "running")
    assert "observer_token_sha256" in result and not target.exists()
    retry = client.local_main(_local_submit_args(tmp_path, tmp_path / "retry.token", "persist-fails"))
    assert retry["id"] == result["id"] and retry["observer_token"].startswith("not issued")
    assert not (tmp_path / "retry.token").exists()
    assert len([r for r in runs.list(False)["runs"] if r.get("request_id") == "persist-fails"]) == 1
    runs.cancel(result["id"], "test/custody")
    assert finished(runs, result)["cleanup"] == "complete"
