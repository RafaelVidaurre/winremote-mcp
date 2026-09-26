"""File-oriented CLI for the real winremote MCP API; no substitute transport."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from winremote.artifacts import digest
from winremote.runs import ACTIVE, Runs


def connection(config: str | None = None):
    if config:
        d = json.loads(Path(config).expanduser().read_text())
        return d["url"], d.get("headers", {})
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    candidates = [
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml",
        Path.home() / ".claude.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        raw = p.read_text()
        if p.suffix == ".toml":
            d = tomllib.loads(raw).get("mcp_servers", {}).get("winremote", {})
            headers = dict(d.get("http_headers", {}))
            for key, var in d.get("env_http_headers", {}).items():
                if var not in os.environ:
                    raise ValueError("configured winremote header environment variable is unavailable")
                headers[key] = os.environ[var]
            bearer = d.get("bearer_token_env_var")
            if bearer:
                headers["Authorization"] = "Bearer " + os.environ[bearer]
        else:
            d = json.loads(raw).get("mcpServers", {}).get("winremote", {})
            headers = dict(d.get("headers", {}))
        if d.get("url"):
            return d["url"], headers
    raise ValueError("No HTTP winremote configuration found; use --config with {url,headers}")


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {}, timeout=90)
    if result.is_error:
        raise RuntimeError(f"{name} failed")
    data = result.structured_content
    if data is not None:
        return data
    return json.loads(next(c.text for c in result.content if hasattr(c, "text")))


async def upload(client, local: Path, remote: str):
    expected = digest(local)
    size = local.stat().st_size
    info = await call(client, "UploadBegin", dict(path=remote, size=size, sha256=expected))
    if info["complete"]:
        return dict(path=remote, size=size, sha256=expected, complete=True)
    with local.open("rb") as f:
        f.seek(info["offset"])
        offset = info["offset"]
        while chunk := f.read(info["chunk_bytes"]):
            receipt = await call(
                client,
                "UploadChunk",
                dict(transfer_id=info["transfer_id"], offset=offset, data_base64=base64.b64encode(chunk).decode()),
            )
            offset = receipt["offset"]
    return await call(client, "UploadFinish", dict(transfer_id=info["transfer_id"]))


async def download(client, remote: str, local: Path):
    info = await call(client, "ArtifactInfo", dict(path=remote))
    local = local.resolve()
    if local.exists():
        if local.stat().st_size == info["size"] and digest(local) == info["sha256"]:
            return dict(path=str(local), size=info["size"], sha256=info["sha256"], complete=True)
        raise ValueError("local destination exists with different content")
    local.parent.mkdir(parents=True, exist_ok=True)
    part = local.with_name(local.name + ".winremote-" + info["sha256"] + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset > info["size"]:
        raise ValueError("partial download exceeds artifact size")
    with part.open("ab") as f:
        while offset < info["size"]:
            chunk = await call(client, "ArtifactRead", dict(path=remote, offset=offset, revision=info["revision"]))
            raw = base64.b64decode(chunk["data_base64"], validate=True)
            if chunk["offset"] != offset or not raw or len(raw) != chunk["bytes"]:
                raise ValueError("invalid artifact chunk")
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
            offset += len(raw)
    if part.stat().st_size != info["size"] or digest(part) != info["sha256"]:
        raise ValueError("download hash/length mismatch; partial file preserved")
    os.link(part, local)
    part.unlink()
    return dict(path=str(local), size=info["size"], sha256=info["sha256"], complete=True)


async def wait(client, run_id, seconds):
    deadline = time.monotonic() + seconds
    while True:
        state = await call(client, "RunStatus", {"run_id": run_id})
        if state["state"] not in ("queued", "running") or time.monotonic() >= deadline:
            return state
        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))


def make_bundle(repo, commit, base, target):
    def git(*args):
        return subprocess.check_output(["git", "-C", repo, *args], text=True, encoding="utf-8").strip()

    sha = git("rev-parse", "--verify", commit + "^{commit}")
    ref = "refs/winremote/" + uuid.uuid4().hex
    git("update-ref", ref, sha)
    try:
        args = ["bundle", "create", str(target), ref]
        if base:
            args += ["^" + git("rev-parse", "--verify", base + "^{commit}")]
        git(*args)
    finally:
        git("update-ref", "-d", ref, sha)
    return sha, ref


async def remote_main(args):
    url, headers = connection(args.config)
    async with Client(StreamableHttpTransport(url, headers=headers), timeout=90) as client:
        if args.action == "upload":
            return await upload(client, Path(args.source), args.destination)
        if args.action == "download":
            return await download(client, args.source, Path(args.destination))
        if args.action == "wait":
            return await wait(client, args.run_id, args.seconds)
        if args.action == "stage":
            host = (await call(client, "RunList"))["machine"]
            with tempfile.TemporaryDirectory(prefix="winremote-bundle-") as temp:
                bundle = Path(temp) / "source.bundle"
                sha, ref = make_bundle(args.source, args.commit, args.base, bundle)
                remote = host["artifact_dir"].rstrip("\\/") + "/source-" + digest(bundle) + ".bundle"
                await upload(client, bundle, remote)
            return await call(
                client,
                "RunSubmit",
                dict(
                    command=[
                        host["python"],
                        "-m",
                        "winremote.source_stage",
                        "--bundle",
                        remote,
                        "--repository",
                        args.repository,
                        "--checkout",
                        args.checkout,
                        "--commit",
                        sha,
                        "--ref",
                        ref,
                    ],
                    cwd=host["home"],
                    owner=args.owner,
                    timeout=120,
                    workspace_access="shared",
                    resources={"path:" + args.repository: "exclusive", "path:" + args.checkout: "exclusive"},
                ),
            )
        if args.action == "call":
            return await call(client, args.tool, json.loads(Path(args.args_file).read_text()) if args.args_file else {})
        return await call(client, *api_request(args))


def api_request(args):
    if args.action == "list":
        return "RunList", {"active_only": not args.all}
    if args.action == "status":
        return "RunStatus", {"run_id": args.run_id}
    if args.action == "cancel":
        return "RunCancel", {"run_id": args.run_id, "owner": args.owner}
    if args.action == "log":
        return "RunLog", {"run_id": args.run_id, "offset": args.offset, "limit": args.limit}
    if args.action == "submit":
        return "RunSubmit", json.loads(Path(args.spec).read_text())
    raise ValueError("unsupported action")


def local_main(args):
    runs = Runs()
    if args.action == "list":
        return runs.list(not args.all)
    if args.action == "status":
        runs.recover()
        return runs.get(args.run_id)
    if args.action == "cancel":
        return runs.cancel(args.run_id, args.owner)
    if args.action == "submit":
        return runs.submit(**json.loads(Path(args.spec).read_text()))
    if args.action == "wait":
        deadline = time.monotonic() + args.seconds
        while True:
            runs.recover()
            state = runs.get(args.run_id)
            if state["state"] not in ("queued", "running") or time.monotonic() >= deadline:
                return state
            time.sleep(0.5)
    raise ValueError("--local supports submit/list/status/cancel/wait")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="MCP connection JSON; otherwise reads existing Codex/Claude winremote config")
    parser.add_argument("--local", action="store_true", help="Use this machine's independent queue")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("submit", help="Submit a JSON RunSubmit specification")
    p.add_argument("spec")
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("run_id")
    p = sub.add_parser("cancel")
    p.add_argument("run_id")
    p.add_argument("--owner", required=True)
    p = sub.add_parser("wait")
    p.add_argument("run_id")
    p.add_argument("--seconds", type=float, default=60)
    p = sub.add_parser("log")
    p.add_argument("run_id")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=8192)
    for action in ("upload", "download"):
        p = sub.add_parser(action)
        p.add_argument("source")
        p.add_argument("destination")
    p = sub.add_parser("stage", help="Upload a verified Git bundle and queue a new detached host checkout")
    p.add_argument("source")
    p.add_argument("--commit", default="HEAD")
    p.add_argument("--base")
    p.add_argument("--repository", required=True)
    p.add_argument("--checkout", required=True)
    p.add_argument("--owner", required=True)
    p = sub.add_parser("call", help="Invoke an actual MCP tool with JSON arguments")
    p.add_argument("tool")
    p.add_argument("--args-file")
    args = parser.parse_args()
    try:
        result = local_main(args) if args.local else asyncio.run(remote_main(args))
        print(json.dumps(result, indent=2))
        if result.get("state") in ("failed", "timeout", "expired", "cancelled", "interrupted", "cleanup_uncertain"):
            return 1
        if args.action == "wait" and result.get("state") in ACTIVE:
            return 3
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
