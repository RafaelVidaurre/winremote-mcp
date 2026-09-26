"""Unit tests for Shell, Scrape, App tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _call_tool(tool_name, **kwargs):
    from winremote import __main__

    tools = {
        "App": __main__.App,
        "Scrape": __main__.Scrape,
        "Shell": __main__.Shell,
    }
    return tools[tool_name](**kwargs)


class TestShell:
    def test_long_commands_require_owned_async_runs(self):
        import pytest
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="RunSubmit"):
            _call_tool("Shell", command="irrelevant", timeout=61)

    def test_native_shell_timeout_cleans_up_and_next_call_works(self):
        import os

        import pytest
        from fastmcp.exceptions import ToolError

        if os.name != "nt":
            pytest.skip("requires native PowerShell and Windows Job Objects")
        with pytest.raises(ToolError, match='"cleanup": "complete"'):
            _call_tool("Shell", command="Start-Sleep 20", timeout=1)
        assert "native hello" in _call_tool("Shell", command="Write-Output 'native hello'", timeout=10)

    def test_native_shell_exit_failure_is_an_mcp_error(self):
        import os

        import pytest
        from fastmcp.exceptions import ToolError

        if os.name != "nt":
            pytest.skip("requires native PowerShell")
        with pytest.raises(ToolError, match='"exit_code": 7'):
            _call_tool("Shell", command="Write-Output 'failed command'; exit 7", timeout=10)


class TestScrape:
    @patch("urllib.request.build_opener")
    def test_scrape_success(self, mock_build_opener, monkeypatch):
        import socket

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html><body><h1>Hello</h1></body></html>"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_build_opener.return_value.open.return_value = mock_resp
        result = _call_tool("Scrape", url="https://example.com")
        assert "Hello" in result or "task:" in result

    def test_scrape_error(self):
        with patch("urllib.request.build_opener", side_effect=Exception("network error")):
            result = _call_tool("Scrape", url="https://bad.url")
            assert "error" in result.lower()


class TestApp:
    @patch("winremote.__main__.desktop")
    def test_app_launch(self, mock_desktop):
        mock_desktop.launch_app.return_value = "Launched notepad"
        result = _call_tool("App", action="launch", name="notepad")
        assert "Launched" in result or "task:" in result

    def test_app_unknown_action(self):
        result = _call_tool("App", action="unknown")
        assert "Unknown" in result or "task:" in result
