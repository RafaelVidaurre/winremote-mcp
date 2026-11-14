"""Durable, machine-local admission and owned background runs.

SQLite transactions arbitrate all clients. Workers survive client/server exits;
older conflicting requests prevent starvation without blocking disjoint work.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import platform
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil

ACTIVE = {"queued", "running", "cleanup_uncertain"}
HELD = {"running", "cleanup_uncertain"}
TERMINAL = {"succeeded", "failed", "cancelled", "timeout", "expired", "interrupted"}


def state_root() -> Path:
    return Path(os.environ.get("WINREMOTE_RUNS_DIR", str(Path.home() / ".local/share/winremote/runs"))).resolve()


def path_resource(path: str) -> str:
    return "path:" + os.path.normcase(os.path.normpath(os.path.realpath(path)))


def resources_conflict(a: dict, b: dict) -> bool:
    for x, xm in a.items():
        for y, ym in b.items():
            if xm == ym == "shared":
                continue
            overlap = x == y
            if x.startswith("path:") and y.startswith("path:"):
                xp, yp = x[5:], y[5:]
                try:
                    overlap = os.path.commonpath([xp, yp]) in (xp, yp)
                except ValueError:
                    pass
            if overlap:
                return True
    return False


def covers(held: dict, claims: dict, output_dir: str | None = None) -> bool:
    """Every claim names something the held run already owns (paths by containment)."""
    owned = [name for name in held if name.startswith("path:")]
    if output_dir:
        owned.append(path_resource(output_dir))
    for name in claims:
        if name.startswith("path:"):
            target = name[5:]
            inside = False
            for root in owned:
                try:
                    inside = os.path.commonpath([target, root[5:]]) == root[5:]
                except ValueError:
                    inside = False
                if inside:
                    break
            if not inside:
                return False
        elif name not in held:
            return False
    return True


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def conflicts(a: dict, b: dict) -> bool:
    return (
        a["isolation"] == "exclusive"
        or b["isolation"] == "exclusive"
        or resources_conflict(a["resources"], b["resources"])
    )


def alive(pid: int | None, birth: float | None) -> bool:
    if not pid or not birth:
        return False
    try:
        p = psutil.Process(pid)
        # Windows has no zombie state. psutil.status() enumerates threads to
        # detect suspension, which must retain custody anyway.
        return abs(p.create_time() - birth) < 0.01 and (
            sys.platform == "win32" or p.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # uncertainty never permits overlapping admission


class Runs:
    def __init__(self, root: Path | None = None):
        self.root = (root or state_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "runs.sqlite3"
        # Ordinary clients read the initialized schema without taking a write lock.
        # Only first use / migration writes; WAL persists in the database itself.
        with contextlib.closing(sqlite3.connect(self.db, timeout=60)) as db:
            initialized = db.execute("PRAGMA user_version").fetchone()[0] >= 1
        if not initialized:
            with contextlib.closing(sqlite3.connect(self.db, timeout=60)) as db:
                db.execute("PRAGMA journal_mode=WAL")
            with self.connect() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS runs "
                    "(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, data TEXT)"
                )
                db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", ("machine_id", uuid.uuid4().hex))
                db.execute("CREATE INDEX IF NOT EXISTS runs_state ON runs(json_extract(data, '$.state'))")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS runs_request ON runs("
                    "json_extract(data, '$.request_id'), json_extract(data, '$.owner'))"
                )
                db.execute("PRAGMA user_version=1")
        with self.connect(write=False) as db:
            machine_id = db.execute("SELECT value FROM metadata WHERE key=?", ("machine_id",)).fetchone()[0]
        self.machine = {
            "id": machine_id,
            "hostname": platform.node(),
            "os": platform.platform(),
            "architecture": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": sys.executable,
            "home": str(Path.home()),
            "artifact_dir": str(self.root / "artifacts"),
        }

    @contextlib.contextmanager
    def connect(self, write=True, timeout=60):
        """One transaction: writers take the write lock up front; readers only read a snapshot."""
        db = sqlite3.connect(self.db, timeout=timeout)
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _rows(db, limit):
        rows = db.execute("SELECT data FROM runs ORDER BY sequence DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    @staticmethod
    def _active_rows(db):
        """The runs admission and recovery reason about, without parsing the whole history."""
        states = sorted(ACTIVE)
        query = "SELECT data FROM runs WHERE json_extract(data, '$.state') IN (%s) ORDER BY sequence" % ",".join(
            "?" * len(states)
        )
        return [json.loads(r[0]) for r in db.execute(query, states)]

    @staticmethod
    def _save(db, run):
        db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run["id"]))

    def get(self, run_id: str) -> dict:
        with self.connect(write=False) as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown run: {run_id}")
        return json.loads(row[0])

    def update(self, run_id: str, **changes) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown run: {run_id}")
            run = json.loads(row[0])
            run.update(changes)
            self._save(db, run)
        return run

    def list(self, active_only=True, limit=50) -> dict:
        self.recover()
        with self.connect(write=False) as db:
            # Active custody is never truncated: omitting an older resident could
            # make clients believe its checkout, desktop or port is free.
            rows = self._active_rows(db) if active_only else self._rows(db, min(max(limit, 1), 200))
        return {"machine": self.machine, "runs": rows}

    def submit(
        self,
        command: list[str],
        cwd: str,
        owner: str,
        *,
        resources: dict | None = None,
        isolation="shared",
        workspace_access="exclusive",
        timeout=60.0,
        queue_timeout=1800.0,
        request_id: str | None = None,
        env: dict | None = None,
        hidden=True,
        spawn=True,
    ) -> dict:
        if not command or any(not isinstance(s, str) or not s or "\x00" in s for s in command):
            raise ValueError("command must be a nonempty argv array")
        if not owner or len(owner) > 200:
            raise ValueError("owner must name the project/session (1..200 characters)")
        if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("cwd must be an existing absolute directory")
        for value in (timeout, queue_timeout):
            if not math.isfinite(value) or not 0 < value <= 86400:
                raise ValueError("timeouts must be finite, positive and <=86400 seconds")
        if isolation not in ("shared", "exclusive") or workspace_access not in ("shared", "exclusive"):
            raise ValueError("isolation and workspace_access must be shared or exclusive")
        claims = {}
        for name, mode in (resources or {}).items():
            if not isinstance(name, str) or not name or len(name) > 1024 or mode not in ("shared", "exclusive"):
                raise ValueError("resources map nonempty names to shared/exclusive")
            if name.startswith("path:"):
                if not Path(name[5:]).is_absolute():
                    raise ValueError("path resources must be absolute")
                name = path_resource(name[5:])
            claims[name] = mode
        workspace = path_resource(cwd)
        claims[workspace] = "exclusive" if claims.get(workspace) == "exclusive" else workspace_access
        if any(
            not isinstance(k, str) or not isinstance(v, str) or "=" in k or "\x00" in k + v
            for k, v in (env or {}).items()
        ):
            raise ValueError("env must contain valid string names and values")
        spec = dict(
            command=command,
            cwd=str(Path(cwd).resolve()),
            owner=owner,
            resources=claims,
            isolation=isolation,
            timeout=timeout,
            queue_timeout=queue_timeout,
            env=env or {},
            hidden=hidden,
        )
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        if request_id is not None and (not request_id or len(request_id) > 200):
            raise ValueError("request_id must have 1..200 characters")
        with self.connect() as db:
            if request_id:
                for (data,) in db.execute(
                    "SELECT data FROM runs WHERE json_extract(data, '$.request_id')=? "
                    "AND json_extract(data, '$.owner')=?",
                    (request_id, owner),
                ):
                    old = json.loads(data)
                    if old["fingerprint"] != fingerprint:
                        raise ValueError("request_id already names a different request")
                    return old
            run_id = uuid.uuid4().hex
            out = self.root / run_id
            out.mkdir()
            # Observation authority is a capability issued with this exact admission.
            # Only its digest is durable, so status, listings and retries cannot reveal it.
            observer_token = secrets.token_urlsafe(32)
            run = dict(
                spec,
                id=run_id,
                request_id=request_id,
                fingerprint=fingerprint,
                state="queued",
                created_at=time.time(),
                machine=self.machine,
                output_dir=str(out),
                cancel_requested=False,
                worker_pid=None,
                worker_birth=None,
                cleanup="pending",
                kind="run",
                observer_token_sha256=token_digest(observer_token),
            )
            db.execute("INSERT INTO runs(id,data) VALUES (?,?)", (run_id, json.dumps(run)))
        if spawn:
            try:
                with (out / "worker.log").open("ab") as log:
                    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
                    if os.name == "nt":
                        # DETACHED_PROCESS can open Windows Terminal for venv Python
                        # redirectors. Keep the independent worker console-free.
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                    else:
                        kwargs["start_new_session"] = True
                    worker = subprocess.Popen(
                        [sys.executable, "-m", "winremote.run_worker", str(self.root), run_id], **kwargs
                    )
            except BaseException as exc:
                self.update(run_id, state="failed", error=str(exc), cleanup="complete", finished_at=time.time())
                raise
            # Once Popen succeeded, a bookkeeping failure must never declare
            # cleanup complete: the worker may already own a running child.
            try:
                birth = psutil.Process(worker.pid).create_time()
            except psutil.NoSuchProcess:
                pass  # It may already have completed; recovery handles failed startup.
            else:
                with self.connect() as db:
                    current = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()[0])
                    if current["state"] == "queued" and not current.get("worker_pid"):
                        current.update(worker_pid=worker.pid, worker_birth=birth)
                        self._save(db, current)
        return dict(self.get(run_id), observer_token=observer_token)

    @staticmethod
    def _admission(rows, run_id):
        current = next((r for r in rows if r["id"] == run_id), None)
        if current is None or current["state"] != "queued":
            return None
        if current["cancel_requested"]:
            return dict(current, state="cancelled", cleanup="complete", finished_at=time.time())
        if time.time() > current["created_at"] + current["queue_timeout"]:
            return dict(current, state="expired", cleanup="complete", finished_at=time.time())
        for other in rows:
            if other["id"] == run_id:
                break
            if conflicts(current, other):
                return None
        if any(r["id"] != run_id and r["state"] in HELD and conflicts(current, r) for r in rows):
            return None
        return dict(current, state="running", started_at=time.time())

    def admit(self, run_id: str) -> bool:
        self.recover()
        # A blocked poll is read-only. Recheck under the writer lock before any
        # grant, so concurrent submitters cannot both acquire a shared resource.
        with self.connect(write=False) as db:
            if self._admission(self._active_rows(db), run_id) is None:
                return False
        with self.connect() as db:
            current = self._admission(self._active_rows(db), run_id)
            if current is None:
                return False
            self._save(db, current)
            return current["state"] == "running"

    def cancel(self, run_id: str, owner: str) -> dict:
        run = self.get(run_id)
        if run["owner"] != owner:
            raise ValueError("owner does not match; use the recorded owner only with authority to cancel that run")
        with self.connect() as db:
            run = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            if run["state"] in ACTIVE:
                run["cancel_requested"] = True
                if run["state"] == "queued":
                    run.update(state="cancelled", cleanup="complete", finished_at=time.time())
                self._save(db, run)
        return run

    def recover(self):
        """Probe outside transactions, then reconcile only an unchanged dead-worker snapshot."""
        with self.connect(write=False) as db:
            snapshots = self._active_rows(db)
        candidates = []
        identities = {}
        for run in snapshots:
            identity = (run.get("worker_pid"), run.get("worker_birth"))
            if identity[0]:
                if identity not in identities:
                    identities[identity] = alive(*identity)
                if identities[identity]:
                    continue
            if not run.get("worker_pid") and time.time() - run["created_at"] < 30:
                continue  # worker startup handshake
            if run["kind"] == "lease" or run["state"] == "queued":
                changes = dict(state="interrupted", cleanup="complete", finished_at=time.time())
            else:
                from winremote.run_worker import tree_absent

                if tree_absent(run):
                    changes = dict(state="interrupted", cleanup="complete", finished_at=time.time())
                else:
                    changes = dict(state="cleanup_uncertain", cleanup="uncertain")
            if any(run.get(k) != v for k, v in changes.items()):
                candidates.append((run, changes))
        if not candidates:
            return
        try:
            # Metadata requests never queue behind writers for housekeeping.
            # Skipping a busy reconciliation retains claims conservatively.
            with self.connect(timeout=0) as db:
                for snapshot, changes in candidates:
                    row = db.execute("SELECT data FROM runs WHERE id=?", (snapshot["id"],)).fetchone()
                    if row and json.loads(row[0]) == snapshot:
                        self._save(db, dict(snapshot, **changes))
        except sqlite3.OperationalError as error:
            if str(error) not in ("database is locked", "database table is locked"):
                # Other database errors must remain visible.
                raise

    @staticmethod
    def record_observation(owner: dict, tool: str, details: dict) -> None:
        """Append a nonsecret receipt correlating an observation with the held run."""
        receipt = dict(
            time=time.time(),
            run_id=owner["id"],
            machine_id=owner["machine"]["id"],
            run_state=owner["state"],
            tool=tool,
            **details,
        )
        with (Path(owner["output_dir"]) / "observations.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(receipt, sort_keys=True) + "\n")

    @staticmethod
    def _observed(rows: list, within: tuple[str, str], claims: dict) -> dict:
        run_id, token = within
        owner = next((r for r in rows if r["id"] == run_id and r.get("kind") == "run"), None)
        expected = (owner or {}).get("observer_token_sha256")
        if not expected or not isinstance(token, str) or not hmac.compare_digest(token_digest(token), expected):
            raise PermissionError("observation_denied: unknown run or observer token")
        if owner["state"] not in HELD:
            raise PermissionError(f"observation_denied: run is {owner['state']}, its claims are not held")
        if not covers(owner["resources"], claims, owner.get("output_dir")):
            raise PermissionError("observation_denied: requested resources are outside the run's claims")
        return owner

    @contextlib.contextmanager
    def operation(self, name: str, resources: dict | None = None, within: tuple[str, str] | None = None):
        """Short existing tools fail fast during conflicts instead of building hidden queues.

        within=(run_id, observer_token) nests a read-only observation inside that run's held
        claims. Only claims the run already owns are admitted; the owning run and queued
        requests do not block it, and every other held conflicting run still does.
        """
        self.recover()
        run = dict(
            id=uuid.uuid4().hex,
            owner="mcp:" + name,
            kind="lease",
            isolation="shared",
            resources=resources or {},
            state="running",
            created_at=time.time(),
            worker_pid=os.getpid(),
            worker_birth=psutil.Process().create_time(),
            cleanup="pending",
        )
        owner = None
        with self.connect() as db:
            rows = self._active_rows(db)
            if within is None:
                blockers = [r for r in rows if r["state"] in ACTIVE and conflicts(run, r)]
            else:
                held = db.execute("SELECT data FROM runs WHERE id=?", (within[0],)).fetchone()
                owner = self._observed([json.loads(held[0])] if held else [], within, run["resources"])
                run["observing"] = owner["id"]
                blockers = [r for r in rows if r["id"] != owner["id"] and r["state"] in HELD and conflicts(run, r)]
            if blockers:
                raise RuntimeError("host_busy: " + ", ".join(r["id"] + " (" + r["owner"] + ")" for r in blockers[:3]))
            db.execute("INSERT INTO runs(id,data) VALUES (?,?)", (run["id"], json.dumps(run)))
        try:
            yield owner
        finally:
            # Short tool calls need admission, not permanent history per binary chunk.
            with self.connect() as db:
                db.execute("DELETE FROM runs WHERE id=?", (run["id"],))
