"""Connect to the real entry server without sending a task or model request."""
import asyncio
from websockets.asyncio.client import connect


async def main():
    for attempt in range(30):
        try:
            async with connect('ws://127.0.0.1:8765', open_timeout=2):
                print('Read-only Linux container WebSocket startup: PASS')
                return
        except (OSError, TimeoutError):
            await asyncio.sleep(1)
    raise SystemExit('Container did not start within 30 seconds')


if __name__ == '__main__':
    asyncio.run(main())
