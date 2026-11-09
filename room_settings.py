"""Durable room preferences changed by the Telegram owner."""
import asyncio
import re
from state_store import StateStore


class RoomSettings:
    def __init__(self, path):
        self.store = StateStore("room-settings", path)
        self.lock = asyncio.Lock()
        try:
            saved = self.store.load() or {"rooms": {}}
            self.rooms = saved["rooms"]
            if not isinstance(self.rooms, dict):
                raise ValueError()
            for room, prefs in self.rooms.items():
                if not re.fullmatch(r"[1-9]\d*-\d+", room) or not isinstance(prefs, dict):
                    raise ValueError()
                for key, value in prefs.items():
                    self.validate(key, value)
        except (OSError, ValueError, TypeError, KeyError):
            raise RuntimeError("Cannot load room settings; repair room-settings storage.") from None

    @staticmethod
    def validate(key, value):
        if key in {"quiet", "welcomes"} and type(value) is bool:
            return
        if key == "welcome" and isinstance(value, str) and len(value) <= 300 and not value.lstrip().startswith(("*", "/", "!")) and not any(ord(c) < 32 for c in value):
            return
        raise ValueError("Use a plain welcome of up to 300 characters. {name} and {room} are supported.")

    def get(self, room):
        return {"quiet": False, "welcomes": True, "welcome": "", **self.rooms.get(room, {})}

    async def set(self, room, key, value):
        await self.update(room, {key: value})

    async def update(self, room, changes):
        for key, value in changes.items():
            self.validate(key, value)
        if not re.fullmatch(r"[1-9]\d*-\d+", room):
            raise ValueError("Invalid room")
        async with self.lock:
            updated = {**self.rooms, room: {**self.rooms.get(room, {}), **changes}}
            await self._save(updated)

    async def remove(self, room):
        async with self.lock:
            if room in self.rooms:
                updated = self.rooms.copy()
                del updated[room]
                await self._save(updated)

    async def _save(self, updated):
        write = asyncio.create_task(asyncio.to_thread(self.store.save, {"version": 1, "rooms": updated}))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            self.rooms = updated
            raise
        self.rooms = updated
