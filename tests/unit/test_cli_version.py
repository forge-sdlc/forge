"""Unit tests for the forge version CLI command."""

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from forge import __version__
from forge.cli import cmd_version, main


class TestCLIVersionParserAndRouting:
    """Parser routing and command execution tests for version."""

    @patch("forge.cli.cmd_version", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_routing_version(self, _mock_setup_logging, mock_cmd):
        """Calling main(['version']) routes to cmd_version."""
        mock_cmd.return_value = 0
        code = main(["version"])
        assert code == 0
        mock_cmd.assert_called_once()
        args = mock_cmd.call_args[0][0]
        assert args.command == "version"

    @pytest.mark.asyncio
    async def test_cmd_version_execution(self, capsys):
        """cmd_version prints the correct version string and exits with 0."""
        args = argparse.Namespace()
        code = await cmd_version(args)
        assert code == 0
        captured = capsys.readouterr()
        assert f"Forge v{__version__}" in captured.out

    @patch("forge.cli.cmd_version", new_callable=AsyncMock)
    @patch("forge.cli.setup_logging")
    def test_routing_version_json(self, _mock_setup_logging, mock_cmd):
        """Calling main(['version', '--json']) routes to cmd_version with args.json=True."""
        mock_cmd.return_value = 0
        code = main(["version", "--json"])
        assert code == 0
        mock_cmd.assert_called_once()
        args = mock_cmd.call_args[0][0]
        assert args.command == "version"
        assert args.json is True

    @pytest.mark.asyncio
    async def test_cmd_version_json_execution(self, capsys):
        """cmd_version with json=True prints compact JSON and exits with 0."""
        import json

        args = argparse.Namespace(json=True)
        code = await cmd_version(args)
        assert code == 0
        captured = capsys.readouterr()

        # Verify it has exactly one trailing newline and is valid JSON
        assert captured.out.endswith("\n")
        assert captured.out.count("\n") == 1

        data = json.loads(captured.out.strip())
        assert data == {"version": __version__}
        assert captured.err == ""

    @pytest.mark.asyncio
    async def test_cmd_version_logging_isolation(self, capsys):
        """When verbose logging is set, log output is routed to stderr, and only JSON goes to stdout."""
        import json
        import logging

        from forge.cli import setup_logging

        # Clean up existing handlers to start fresh
        root_logger = logging.getLogger()
        old_handlers = list(root_logger.handlers)
        old_level = root_logger.level
        root_logger.handlers.clear()

        try:
            setup_logging(verbose=True)
            logger = logging.getLogger("test_cli_version")
            logger.info("This is an info log message")
            logger.debug("This is a debug log message")

            args = argparse.Namespace(json=True)
            code = await cmd_version(args)
            assert code == 0

            captured = capsys.readouterr()

            # Assert stdout has ONLY the json payload
            stdout_lines = captured.out.strip().split("\n")
            assert len(stdout_lines) == 1
            data = json.loads(stdout_lines[0])
            assert data == {"version": __version__}

            # Assert stderr has the log messages
            assert "This is an info log message" in captured.err
            assert "This is a debug log message" in captured.err
        finally:
            # Restore handlers and level
            root_logger.handlers.clear()
            for h in old_handlers:
                root_logger.addHandler(h)
            root_logger.setLevel(old_level)

    @pytest.mark.asyncio
    async def test_cmd_version_no_json_attribute_defaults_to_text(self, capsys):
        """When args does not contain a 'json' attribute, cmd_version defaults to plain text."""
        args = argparse.Namespace()
        code = await cmd_version(args)
        assert code == 0
        captured = capsys.readouterr()
        assert captured.out == f"Forge v{__version__}\n"

    @pytest.mark.asyncio
    async def test_cmd_version_json_explicit_false(self, capsys):
        """When args has 'json' explicitly set to False, cmd_version prints plain text."""
        args = argparse.Namespace(json=False)
        code = await cmd_version(args)
        assert code == 0
        captured = capsys.readouterr()
        assert captured.out == f"Forge v{__version__}\n"

    @patch("forge.cli.setup_logging")
    def test_main_version_json_isolated(self, mock_setup_logging, capsys):
        """Calling main(['-v', 'version', '--json']) prints the correct compact json to stdout and exits 0."""
        import json

        code = main(["-v", "version", "--json"])
        assert code == 0
        mock_setup_logging.assert_called_once_with(True)
        captured = capsys.readouterr()

        # Verify it has exactly one trailing newline and is valid JSON
        assert captured.out.endswith("\n")
        assert captured.out.count("\n") == 1

        data = json.loads(captured.out.strip())
        assert data == {"version": __version__}
        assert captured.err == ""

    def test_main_version_plain_text(self, capsys):
        """Calling main(['version']) prints the correct plain text to stdout and exits 0."""
        code = main(["version"])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.out == f"Forge v{__version__}\n"

    def test_main_version_verbose_plain_text(self, capsys):
        """Calling main(['-v', 'version']) prints the correct plain text to stdout and exits 0."""
        code = main(["-v", "version"])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.out == f"Forge v{__version__}\n"
