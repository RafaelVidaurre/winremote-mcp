"""Detached worker and gated child for winremote owned runs."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from winremote.runs import Runs, alive


class WindowsJob:
    def __init__(self, run_id: str):
        import win32job

        self.api = win32job
        self.handle = win32job.CreateJobObject(None, "Local\\winremote-" + run_id)
        info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)

    def assign(self, pid: int):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x0100 | 0x0001, 0, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.api.AssignProcessToJobObject(self.handle, int(handle))
        finally:
            kernel.CloseHandle(handle)

    def count(self):
        return self.api.QueryInformationJobObject(self.handle, self.api.JobObjectBasicAccountingInformation)[
            "ActiveProcesses"
        ]

    def stop(self):
        self.api.TerminateJobObject(self.handle, 1)

    def close(self):
        self.handle.Close()


def tree_absent(run: dict) -> bool:
    if os.name == "nt":
        import pywintypes
        import win32job

        try:
            handle = win32job.OpenJobObject(0x0004, False, "Local\\winremote-" + run["id"])
        except pywintypes.error as exc:
            return exc.winerror == 2  # only missing object proves absence
        try:
            return (
                win32job.QueryInformationJobObject(handle, win32job.JobObjectBasicAccountingInformation)[
                    "ActiveProcesses"
                ]
                == 0
            )
        finally:
            handle.Close()
    pgid = run.get("child_pid")
    if not pgid:
        return True
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def execute(root: Path, run_id: str):
    if os.name != "nt" and signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise RuntimeError("SIGCHLD must retain child exit status for safe process-group cleanup")
    runs = Runs(root)
    runs.update(run_id, worker_pid=os.getpid(), worker_birth=psutil.Process().create_time())
    while not runs.admit(run_id):
        if runs.get(run_id)["state"] != "queued":
            return
        time.sleep(0.2)
    run = runs.get(run_id)
    out = Path(run["output_dir"])
    manifest = out / "request.json"
    manifest.write_text(json.dumps(run), encoding="utf-8")
    child = job = None
    status, error, code = "failed", None, None
    cleaned = False
    cleanup_error = None
    try:
        if os.name == "nt":
            job = WindowsJob(run_id)
        with (out / "output.log").open("xb") as log:
            kwargs = dict(cwd=run["cwd"], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW if run["hidden"] else subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                kwargs["start_new_session"] = True
            child = subprocess.Popen([sys.executable, "-m", "winremote.run_worker", "--child", str(manifest)], **kwargs)
            if job:
                try:
                    job.assign(child.pid)
                except BaseException:
                    child.kill()
                    child.wait(timeout=5)
                    raise
            runs.update(run_id, child_pid=child.pid, child_birth=psutil.Process(child.pid).create_time())
            child.stdin.write(b"go\n")
            child.stdin.close()
            deadline = time.monotonic() + run["timeout"]
            while True:
                # Keep the POSIX group leader unreaped until group cleanup.
                if os.name == "nt":
                    ended = child.poll() is not None
                else:
                    ended = psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE
                if ended:
                    status = "exited"
                    break
                if runs.get(run_id)["cancel_requested"]:
                    status = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    status = "timeout"
                    break
                time.sleep(0.1)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        errors = []
        if job:
            try:
                job.stop()
                until = time.monotonic() + 5
                while job.count() and time.monotonic() < until:
                    time.sleep(0.05)
                cleaned = job.count() == 0
            except BaseException as exc:
                errors.append(f"job termination: {type(exc).__name__}: {exc}")
        elif child:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                errors.append(f"group signal: {type(exc).__name__}: {exc}")
        if child:
            try:
                code = child.wait(timeout=5)
            except BaseException as exc:
                errors.append(f"child reap: {type(exc).__name__}: {exc}")
        if not job:
            observed = dict(run, child_pid=child.pid if child else None)
            until = time.monotonic() + 5
            while not tree_absent(observed) and time.monotonic() < until:
                time.sleep(0.05)
            cleaned = tree_absent(observed) and (child is None or child.returncode is not None)
        if job:
            job.close()
        cleanup_error = "; ".join(errors) or None
        if status == "exited":
            status = "succeeded" if code == 0 else "failed"
        if not cleaned:
            status = "cleanup_uncertain"
        result = runs.update(
            run_id,
            state=status,
            exit_code=code,
            error=error,
            cleanup="complete" if cleaned else "uncertain",
            cleanup_error=cleanup_error,
            finished_at=time.time(),
        )
        (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def child_main(manifest: Path):
    if sys.stdin.readline() != "go\n":
        return 125
    run = json.loads(manifest.read_text(encoding="utf-8"))
    env = os.environ.copy()
    env.update(run["env"])
    # Source tooling must never create a credential UI or wait for interactive input.
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_ASKPASS="", SSH_ASKPASS="")
    kwargs = {}
    if os.name == "nt" and run["hidden"]:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        kwargs["startupinfo"] = info
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    target = subprocess.Popen(run["command"], stdin=subprocess.DEVNULL, env=env, **kwargs)
    while target.poll() is None:
        if os.name != "nt" and not alive(run["worker_pid"], run["worker_birth"]):
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.1)
    return target.returncode


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        sys.exit(child_main(Path(sys.argv[2])))
    execute(Path(sys.argv[1]), sys.argv[2])
