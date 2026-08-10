from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.cli import cmd_version, main


@patch("forge.cli.cmd_version", new_callable=AsyncMock)
@patch("forge.cli.setup_logging")
def test_routing_version(_mock_setup_logging: MagicMock, mock_cmd: AsyncMock) -> None:
    mock_cmd.return_value = 0
    code = main(["version"])
    assert code == 0
    mock_cmd.assert_called_once()
    args = mock_cmd.call_args[0][0]
    assert args.command == "version"


@pytest.mark.asyncio
async def test_cmd_version_output(capsys: Any) -> None:
    import argparse

    args = argparse.Namespace(command="version")
    code = await cmd_version(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "Forge version:" in captured.out
