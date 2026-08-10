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
