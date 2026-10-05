"""Native MCP surface for durable runs and binary artifacts."""

from __future__ import annotations

import base64
import functools
import hashlib
import inspect
import json
import os
import time
from pathlib import Path

from fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from winremote.artifacts import Artifacts
from winremote.runs import Runs, path_resource

RUN_TOOLS = {
    "RunSubmit",
    "RunStatus",
    "RunCancel",
    "RunList",
    "RunLog",
    "RunSnapshot",
    "UploadBegin",
    "UploadChunk",
    "UploadFinish",
    "ArtifactInfo",
    "ArtifactRead",
}


def register(mcp):
    @mcp.tool()
    def RunSubmit(
        command: list[str],
        cwd: str,
        owner: str,
        resources: dict[str, str] | None = None,
        isolation: str = "shared",
        workspace_access: str = "exclusive",
        timeout: float = 60,
        queue_timeout: float = 1800,
        request_id: str | None = None,
        env: dict[str, str] | None = None,
        hidden: bool = True,
    ) -> dict:
        """Queue an owned argv command and return immediately. Compatible resource claims overlap.

        Cwd is exclusive by default; shared requires an immutable workspace and isolated outputs.
        isolation=exclusive drains earlier runs and blocks later ones for whole-machine measurements.
        Name shared/exclusive resources for ports, desktop, devices and mutable paths (path:absolute).
        owner identifies project/session. Reuse request_id only to retry the identical submission.
        timeout bounds execution; queue_timeout bounds waiting. Completion requires cleanup=complete.
        """
        return Runs().submit(
            command,
            cwd,
            owner,
            resources=resources,
            isolation=isolation,
            workspace_access=workspace_access,
            timeout=timeout,
            queue_timeout=queue_timeout,
            request_id=request_id,
            env=env,
            hidden=hidden,
        )

    @mcp.tool()
    def RunStatus(run_id: str) -> dict:
        """Read durable run status, machine identity, output paths and verified cleanup."""
        runs = Runs()
        runs.recover()
        return runs.get(run_id)

    @mcp.tool()
    def RunList(active_only: bool = True, limit: int = 50) -> dict:
        """Read machine identity and queue before any host workload. Available during exclusive runs."""
        return Runs().list(active_only, limit)

    @mcp.tool()
    def RunCancel(run_id: str, owner: str) -> dict:
        """Request cancellation of the named owned run. Inspect RunStatus until cleanup completes."""
        return Runs().cancel(run_id, owner)

    @mcp.tool()
    def RunLog(
        run_id: str,
        offset: int = 0,
        limit: int = 8192,
        path: str | None = None,
        observer_token: str | None = None,
    ) -> dict:
        """Read a bounded stdout/stderr excerpt by byte offset. Full bytes stay in output.log.

        With path and the run's observer_token, tail a live file inside that held run's own
        path claims (for example an application log) without waiting for the run to release.
        """
        if offset < 0 or not 1 <= limit <= 65536:
            raise ValueError("offset >=0 and limit 1..65536 required")
        runs = Runs()
        if path is None:
            log = Path(runs.get(run_id)["output_dir"]) / "output.log"
            if not log.exists():
                return {"text": "", "next_offset": offset}
            with log.open("rb") as f:
                f.seek(offset)
                raw = f.read(limit)
            return {"text": raw.decode("utf-8", errors="replace"), "next_offset": offset + len(raw)}
        if not Path(path).is_absolute():
            raise ValueError("path must be absolute")
        target = Path(path).resolve()
        with runs.operation("RunLog", {path_resource(str(target)): "shared"}, within=(run_id, observer_token)) as owner:
            if not target.exists():
                raw, size = b"", 0
            else:
                with target.open("rb") as f:
                    f.seek(offset)
                    raw = f.read(limit)
                size = target.stat().st_size
            Runs.record_observation(
                owner,
                "RunLog",
                dict(path=str(target), offset=offset, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()),
            )
        return {"text": raw.decode("utf-8", errors="replace"), "next_offset": offset + len(raw), "size": size}

    @mcp.tool()
    def RunSnapshot(
        run_id: str,
        observer_token: str,
        monitor: int = 0,
        max_width: int = 0,
        quality: int = 75,
    ) -> list:
        """Read-only screenshot and window list of the desktop held by this exact running run.

        Requires the observer_token returned once by RunSubmit and a desktop claim held by
        that run. Other owners' held claims still return host_busy; no input is sent.
        """
        from winremote import desktop

        if not 1 <= quality <= 100 or max_width < 0 or monitor < 0:
            raise ValueError("quality 1..100, max_width >=0 and monitor >=0 required")
        runs = Runs()
        with runs.operation("RunSnapshot", {"desktop": "exclusive"}, within=(run_id, observer_token)) as owner:
            image = desktop.take_screenshot(quality=quality, max_width=max_width, monitor=monitor)
            windows = [
                dict(handle=w.handle, title=w.title, width=w.width, height=w.height, x=w.rect[0], y=w.rect[1])
                for w in desktop.enumerate_windows()
            ]
            receipt = dict(
                monitor=monitor,
                max_width=max_width,
                jpeg_bytes=len(base64.b64decode(image)),
                sha256=hashlib.sha256(base64.b64decode(image)).hexdigest(),
                window_count=len(windows),
            )
            Runs.record_observation(owner, "RunSnapshot", receipt)
        summary = dict(receipt, run_id=run_id, machine_id=owner["machine"]["id"], windows=windows)
        return [
            ImageContent(type="image", data=image, mimeType="image/jpeg"),
            TextContent(type="text", text=json.dumps(summary)),
        ]

    @mcp.tool()
    def UploadBegin(path: str, size: int, sha256: str) -> dict:
        """Begin/resume an immutable artifact upload; returns received offset and chunk_bytes."""
        return Artifacts(Runs()).begin(path, size, sha256)

    @mcp.tool()
    def UploadChunk(transfer_id: str, offset: int, data_base64: str) -> dict:
        """Append one exact binary chunk. Use the MCP client to stream local files, not model text."""
        return Artifacts(Runs()).chunk(transfer_id, offset, data_base64)

    @mcp.tool()
    def UploadFinish(transfer_id: str) -> dict:
        """Verify full length/hash and atomically publish an uploaded artifact without overwrite."""
        return Artifacts(Runs()).finish(transfer_id)

    @mcp.tool()
    def ArtifactInfo(path: str) -> dict:
        """Get artifact size, SHA-256 and revision for a verified chunked download."""
        return Artifacts(Runs()).info(path)

    @mcp.tool()
    def ArtifactRead(path: str, offset: int, revision: str) -> dict:
        """Read a bounded binary chunk at an offset, rejecting source revision changes."""
        return Artifacts(Runs()).read(path, offset, revision)


def protect_tool(name, function, category):
    """Existing MCP tools participate in admission, including exclusive-window barriers."""

    @functools.wraps(function)
    def guarded(*args, **kwargs):
        values = inspect.signature(function).bind(*args, **kwargs).arguments
        claims = {}
        if category == "desktop":
            claims["desktop"] = "exclusive"
        if values.get("path"):
            claims[path_resource(values["path"])] = "exclusive" if name in ("FileWrite", "FileUpload") else "shared"
        with Runs().operation(name, claims):
            return function(*args, **kwargs)

    return guarded


def shell(command: str, timeout: int, cwd: str) -> str:
    """A short synchronous Shell uses exactly the same owned process lifecycle."""
    import json

    from winremote.taskmanager import current_task

    if not 1 <= timeout <= 60:
        raise ToolError("Shell is for bounded commands (1..60s); use RunSubmit for longer work")
    runs = Runs()
    # Reject an exclusive reservation immediately instead of queueing invisible probes.
    with runs.operation("Shell admission"):
        pass
    current = current_task()
    owner = "mcp:Shell:" + (current.task_id if current else str(os.getpid()))
    run = runs.submit(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd or os.getcwd(),
        owner,
        timeout=timeout,
        queue_timeout=timeout,
        workspace_access="exclusive",
    )
    if current:
        current.run_id = run["id"]
    deadline = time.monotonic() + timeout * 2 + 15
    while time.monotonic() < deadline:
        state = runs.get(run["id"])
        if state["state"] not in ("queued", "running"):
            path = Path(state["output_dir"]) / "output.log"
            output = path.read_bytes()[:65536].decode("utf-8", errors="replace") if path.exists() else ""
            receipt = {
                "run_id": run["id"],
                "state": state["state"],
                "exit_code": state.get("exit_code"),
                "cleanup": state["cleanup"],
                "output_dir": state["output_dir"],
            }
            if state["state"] != "succeeded":
                raise ToolError(json.dumps(receipt) + "\n" + output)
            return json.dumps(receipt) + "\n" + (output.strip() or "(no output)")
        if current and current.is_cancelled:
            runs.cancel(run["id"], owner)
        time.sleep(0.1)
    runs.cancel(run["id"], owner)
    raise ToolError(f"Shell observation deadline; inspect RunStatus {run['id']}; cleanup not yet proved")
