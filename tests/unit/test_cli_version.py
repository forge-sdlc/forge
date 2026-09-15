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

    def test_setup_logging_clears_handlers_and_routes_to_stderr(self):
        """setup_logging clears existing handlers and attaches a StreamHandler(sys.stderr) with correct level and formatter."""
        import logging
        import sys

        from forge.cli import setup_logging

        root_logger = logging.getLogger()
        old_handlers = list(root_logger.handlers)
        old_level = root_logger.level
        root_logger.handlers.clear()

        # Add a dummy handler to verify it gets cleared
        dummy_handler = logging.NullHandler()
        root_logger.addHandler(dummy_handler)
        assert dummy_handler in root_logger.handlers

        try:
            # Test non-verbose logging setup
            setup_logging(verbose=False)
            assert dummy_handler not in root_logger.handlers
            assert len(root_logger.handlers) == 1
            handler = root_logger.handlers[0]
            assert isinstance(handler, logging.StreamHandler)
            assert handler.stream is sys.stderr
            assert root_logger.level == logging.INFO
            assert handler.level == logging.INFO
            assert handler.formatter is not None
            assert handler.formatter._fmt == "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

            # Test verbose logging setup
            setup_logging(verbose=True)
            assert len(root_logger.handlers) == 1
            handler = root_logger.handlers[0]
            assert isinstance(handler, logging.StreamHandler)
            assert handler.stream is sys.stderr
            assert root_logger.level == logging.DEBUG
            assert handler.level == logging.DEBUG
        finally:
            root_logger.handlers.clear()
            for h in old_handlers:
                root_logger.addHandler(h)
            root_logger.setLevel(old_level)

    def test_setup_logging_configures_auxiliary_loggers(self):
        """setup_logging ensures auxiliary loggers with StreamHandler(sys.stdout) are rerouted to sys.stderr."""
        import logging
        import sys

        from forge.cli import setup_logging

        root_logger = logging.getLogger()
        old_handlers = list(root_logger.handlers)
        old_level = root_logger.level
        root_logger.handlers.clear()

        # Set up an auxiliary logger with a stdout handler
        aux_logger = logging.getLogger("test_auxiliary_logger")
        aux_handler = logging.StreamHandler(sys.stdout)
        aux_logger.addHandler(aux_handler)

        # Keep track of old state of the auxiliary logger
        old_aux_handlers = list(aux_logger.handlers)
        old_aux_propagate = aux_logger.propagate

        try:
            # First, check behavior when propagate is True
            aux_logger.propagate = True
            setup_logging(verbose=False)

            # The stdout handler should have been removed because propagate is True
            assert aux_handler not in aux_logger.handlers

            # Re-add and set propagate to False
            aux_logger.addHandler(aux_handler)
            aux_logger.propagate = False

            setup_logging(verbose=False)

            # The stdout handler stream should have been redirected to sys.stderr
            assert aux_handler in aux_logger.handlers
            assert aux_handler.stream is sys.stderr

        finally:
            # Restore
            aux_logger.handlers.clear()
            for h in old_aux_handlers:
                # Make sure to reset stream if we mutated it
                if isinstance(h, logging.StreamHandler):
                    h.stream = sys.stdout
                aux_logger.addHandler(h)
            aux_logger.propagate = old_aux_propagate

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

    @patch("forge.cli.setup_logging")
    def test_main_version_plain_text(self, _mock_setup_logging, capsys):
        """Calling main(['version']) prints the correct plain text to stdout and exits 0."""
        code = main(["version"])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.out == f"Forge v{__version__}\n"

    @patch("forge.cli.setup_logging")
    def test_main_version_verbose_plain_text(self, _mock_setup_logging, capsys):
        """Calling main(['-v', 'version']) prints the correct plain text to stdout and exits 0."""
        code = main(["-v", "version"])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.out == f"Forge v{__version__}\n"
