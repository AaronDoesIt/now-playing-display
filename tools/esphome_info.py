"""Read-only: ask an ESPHome device what it is (device info + entity list) over its native API.
Usage: python esphome_info.py 10.0.0.115
"""
import asyncio
import sys

from aioesphomeapi import APIClient

HOST = sys.argv[1]
FIELDS = ("name", "friendly_name", "mac_address", "esphome_version", "compilation_time", "model", "manufacturer",
          "project_name", "project_version", "webserver_port", "uses_password", "api_encryption_supported",
          "suggested_area", "has_deep_sleep")


async def main():
    c = APIClient(HOST, 6053, None)
    try:
        try:
            await asyncio.wait_for(c.connect(login=True), 15)
        except TypeError:
            await asyncio.wait_for(c.connect(), 15)
    except Exception as e:  # noqa: BLE001
        print("connect failed:", type(e).__name__, e)
        return
    try:
        info = await asyncio.wait_for(c.device_info(), 10)
        print("== device info")
        for f in FIELDS:
            if hasattr(info, f):
                print(f"   {f} = {getattr(info, f)!r}")
        ents, svcs = await asyncio.wait_for(c.list_entities_services(), 15)
        print(f"== {len(ents)} entities, {len(svcs)} services")
        for e in ents[:80]:
            kind = type(e).__name__.replace("Info", "")
            print(f"   {kind:14s} {getattr(e, 'object_id', ''):40s} {getattr(e, 'name', '')}")
        for s in svcs[:20]:
            print(f"   service        {getattr(s, 'name', '')}")
    except Exception as e:  # noqa: BLE001
        print("query failed:", type(e).__name__, e)
    finally:
        try:
            await c.disconnect()
        except Exception:  # noqa: BLE001
            pass


asyncio.run(main())
