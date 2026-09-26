"""Resumable binary artifacts over MCP; bytes never need to enter model context."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path

from winremote.runs import Runs, path_resource

CHUNK_BYTES = 512 * 1024


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Artifacts:
    def __init__(self, runs: Runs):
        self.runs = runs
        self.root = runs.root / "transfers"
        self.root.mkdir(exist_ok=True)

    def _meta(self, transfer_id):
        if not re.fullmatch("[a-f0-9]{64}", transfer_id):
            raise ValueError("invalid transfer_id")
        return self.root / (transfer_id + ".json")

    def begin(self, path: str, size: int, sha256: str) -> dict:
        if not Path(path).is_absolute() or not 0 <= size <= 100 * 1024**3 or not re.fullmatch("[a-f0-9]{64}", sha256):
            raise ValueError("upload requires an absolute path, size <=100GB and lowercase SHA-256")
        target = Path(path).resolve()
        transfer_id = hashlib.sha256(f"{target}\n{size}\n{sha256}".encode()).hexdigest()
        with self.runs.operation("UploadBegin", {path_resource(str(target)): "exclusive"}):
            if target.exists():
                if target.is_file() and target.stat().st_size == size and digest(target) == sha256:
                    return {"transfer_id": transfer_id, "offset": size, "complete": True, "sha256": sha256}
                raise ValueError("destination exists with different content; choose a new artifact path")
            target.parent.mkdir(parents=True, exist_ok=True)
            meta = self._meta(transfer_id)
            part = target.parent / (".winremote-" + transfer_id + ".part")
            if not meta.exists():
                info = dict(path=str(target), part=str(part), size=size, sha256=sha256)
                meta.write_text(json.dumps(info), encoding="utf-8")
            if not part.exists():
                part.touch(exist_ok=False)
            return {
                "transfer_id": transfer_id,
                "offset": part.stat().st_size,
                "complete": False,
                "chunk_bytes": CHUNK_BYTES,
            }

    def chunk(self, transfer_id: str, offset: int, data_base64: str) -> dict:
        if len(data_base64) > (CHUNK_BYTES + 2) // 3 * 4:
            raise ValueError("chunk exceeds advertised chunk_bytes")
        data = base64.b64decode(data_base64, validate=True)
        info = json.loads(self._meta(transfer_id).read_text(encoding="utf-8"))
        part = Path(info["part"])
        with self.runs.operation("UploadChunk", {path_resource(str(part)): "exclusive"}):
            with part.open("r+b") as f:
                f.seek(0, 2)
                if offset != f.tell() or offset + len(data) > info["size"]:
                    raise ValueError(f"offset mismatch: resume with UploadBegin; received={f.tell()}")
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                return {"transfer_id": transfer_id, "offset": f.tell()}

    def finish(self, transfer_id: str) -> dict:
        info = json.loads(self._meta(transfer_id).read_text(encoding="utf-8"))
        part, target = Path(info["part"]), Path(info["path"])
        claims = {path_resource(str(part)): "exclusive", path_resource(str(target)): "exclusive"}
        with self.runs.operation("UploadFinish", claims):
            if target.exists():
                if target.stat().st_size == info["size"] and digest(target) == info["sha256"]:
                    return dict(path=str(target), size=info["size"], sha256=info["sha256"], complete=True)
                raise ValueError("destination changed during transfer")
            if part.stat().st_size != info["size"] or digest(part) != info["sha256"]:
                raise ValueError("artifact hash/length mismatch; partial upload preserved for diagnosis")
            # Hard-link publication is atomic and refuses a concurrently created destination.
            os.link(part, target)
            part.unlink()
            return dict(path=str(target), size=info["size"], sha256=info["sha256"], complete=True)

    def info(self, path: str) -> dict:
        target = Path(path).resolve()
        with self.runs.operation("ArtifactInfo", {path_resource(str(target)): "shared"}):
            st = target.stat()
            return dict(
                path=str(target),
                size=st.st_size,
                sha256=digest(target),
                chunk_bytes=CHUNK_BYTES,
                revision=f"{st.st_size}:{st.st_mtime_ns}",
            )

    def read(self, path: str, offset: int, revision: str) -> dict:
        target = Path(path).resolve()
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        with self.runs.operation("ArtifactRead", {path_resource(str(target)): "shared"}):
            st = target.stat()
            if revision != f"{st.st_size}:{st.st_mtime_ns}":
                raise ValueError("artifact changed during download")
            with target.open("rb") as f:
                f.seek(offset)
                data = f.read(CHUNK_BYTES)
            return dict(offset=offset, data_base64=base64.b64encode(data).decode(), bytes=len(data))
