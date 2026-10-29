"""Kill a real worker process and resume its Redis checkpoint in a fresh process."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from forge.queue.consumer import TICKET_LOCK_KEY_PREFIX


@pytest.mark.parametrize(
    "boundary", ["accepted", "prepared", "checkpoint", "mid_graph", "graph_done", "applied"]
)
async def test_process_death_at_command_boundary_converges(redis_client, redis_url, boundary):
    helper = Path(__file__).with_name("processing_crash_worker.py")

    async def run(point):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(helper),
            redis_url,
            point,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[3] / "src")},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, error = await asyncio.wait_for(process.communicate(), timeout=60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        return process.returncode, output.decode() + error.decode()

    code, output = await run(boundary)
    assert code == 86, output
    # Exercise real key expiry without waiting for the ten-minute production lease.
    await redis_client.pexpire(f"{TICKET_LOCK_KEY_PREFIX}CRASH-901", 1)
    await asyncio.sleep(0.01)
    code, output = await run("recover")
    assert code == 0, output
    code, output = await run("redeliver")
    assert code == 0, output
    assert int(await redis_client.get("test:transition-calls")) == 1
    expected_station_calls = 2 if boundary == "mid_graph" else 1
    assert int(await redis_client.get("test:station-calls")) == expected_station_calls
