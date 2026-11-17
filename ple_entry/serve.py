"""PLE transport entrypoint, with no workflow or additional task clock."""
import importlib
import inspect
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import taskbridge


async def solve(session):
    target = os.environ.get('FBS_PLE_ENTRYPOINT', '')
    if not target or target.count(':') != 1:
        raise RuntimeError('PR5 PLE task adapter is not configured: set FBS_PLE_ENTRYPOINT=module:function')
    module, name = target.split(':')
    handler = getattr(importlib.import_module(module), name)
    if not inspect.iscoroutinefunction(handler):
        raise TypeError('FBS_PLE_ENTRYPOINT must name an async session handler')
    # PR5's configured adapter owns tasks, acceptance, concurrency and retries.
    # Pass through the official remaining task time; add no per-stage limits.
    return await handler(session)


if __name__ == '__main__':
    taskbridge.run(solve)
