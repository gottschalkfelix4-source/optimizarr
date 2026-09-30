"""Application-owned tasks with one shutdown boundary."""
from __future__ import annotations
import asyncio
import logging

log = logging.getLogger(__name__)
tasks: set[asyncio.Task] = set()
stopping = False


def spawn(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def run_in_thread(function, *args):
    """Cancellation waits for filesystem/DB side effects to actually finish."""
    work = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await work
        raise


async def shutdown(grace: float = 20) -> None:
    global stopping
    stopping = True
    pending = set(tasks)
    if not pending:
        return
    _, pending = await asyncio.wait(pending, timeout=grace)
    for task in pending:
        task.cancel()
    if pending:
        # Cooperative disk workers finish before their parent releases ownership.
        await asyncio.gather(*pending, return_exceptions=True)
