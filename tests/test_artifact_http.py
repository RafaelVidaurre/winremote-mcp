"""Binary transfers through real HTTP/SSE framing, not the in-process shortcut."""

import asyncio
import inspect
import json
import os

import fastmcp
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.tests import run_server_async

from winremote.artifacts import digest
from winremote.client import download
from winremote.run_tools import register


@pytest.mark.parametrize("resume_bytes", [0, 12345])
def test_download_and_resume_fit_real_sse_events(tmp_path, monkeypatch, resume_bytes):
    monkeypatch.setenv("WINREMOTE_RUNS_DIR", str(tmp_path / "queue"))
    monkeypatch.setattr(fastmcp.settings, "json_response", False)
    source = tmp_path / "source.bin"
    source.write_bytes(os.urandom(1200000))
    target = tmp_path / "download.bin"
    # Resume at a non-boundary offset; all remaining chunks must fit the wire cap.
    part = target.with_name(target.name + ".winremote-" + digest(source) + ".part")
    part.write_bytes(source.read_bytes()[:resume_bytes])
    server = FastMCP("artifact-http")
    register(server)

    async def exercise():
        async with run_server_async(server) as url:
            # v3 clients already use the legacy handshake; modern clients must
            # explicitly exercise the deployed server's duplicated reply format.
            options = {"mode": "legacy"} if "mode" in inspect.signature(Client).parameters else {}
            async with Client(StreamableHttpTransport(url), **options) as client:
                info = await client.call_tool("ArtifactInfo", {"path": str(source)})
                chunk = await client.call_tool(
                    "ArtifactRead",
                    {"path": str(source), "offset": resume_bytes, "revision": info.structured_content["revision"]},
                )
                envelope = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [item.model_dump(exclude_none=True) for item in chunk.content],
                        "structuredContent": chunk.structured_content,
                        "isError": False,
                    },
                }
                assert len(json.dumps(envelope).encode()) < 1024 * 1024 - 1024
                result = await download(client, str(source), target)
                assert result["complete"]
                assert result["sha256"] == digest(source)

    asyncio.run(exercise())
    assert target.read_bytes() == source.read_bytes()
    assert not part.exists()
