"""Start a run file, and print each item of its live feed as it comes: the
item's number and type, the event or the status, and the keys of the event's
data that the viewer may see. Times and IDs differ on each run, so they are
left out.
    follow.py RUN_FILE"""
import asyncio
import sys

from provcat import Catalog

from provrun import RunFile
from provrun.live import start_run


async def main() -> None:
    live = start_run(RunFile.load(sys.argv[1]), catalog=Catalog.open())
    async for item in live.follow():
        print(item["n"], item["type"], item.get("event") or item.get("status") or "", *sorted(item.get("data") or {}))
    await live.result()


asyncio.run(main())
