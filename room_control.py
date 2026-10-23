"""Room lifecycle and private local terminal control (never an HTTP admin API)."""
import asyncio
import contextlib
import logging
import os
import re
import sys
import stat

from aiohttp import web
from yarl import URL

from bot import API, Bot, Radio
from permissions import Permissions

LOG = logging.getLogger("ramsei.rooms")


class Authentication:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.saved = None
        self.failure = None

    async def login(self, bot, force=False):
        async with self.lock:
            if self.failure is not None:
                raise self.failure
            # Reuse the account login across rooms, including concurrent joins.
            stale = force and self.saved and bot.session.headers.get("X-Imvu-Sauce") == self.saved[2]
            if self.saved is None or stale:
                try:
                    await bot._login(force=force)
                except Exception as error:
                    self.failure = error
                    raise
                self.saved = (bot.user_id,
                    {k: v.value for k, v in bot.session.cookie_jar.filter_cookies(URL(API)).items()},
                    bot.session.headers["X-Imvu-Sauce"])
            uid, cookies, sauce = self.saved
            bot.user_id = uid
            bot.session.cookie_jar.update_cookies(cookies, response_url=URL(API))
            bot.cookie = bot.session.cookie_jar.filter_cookies(URL(API)).get("osCsid")
            bot.session.headers.update({"X-Imvu-Sauce": sauce, "X-Imvu-Application": "next_desktop/1"})


class RoomManager:
    def __init__(self):
        self.permissions = Permissions()
        self.authentication = Authentication()
        self.rooms = {}
        self.default_room = os.getenv("IMVU_ROOM_ID", "330286535-177")

    async def join(self, room):
        if not re.fullmatch(r"[1-9]\d*-\d+", room):
            raise ValueError("Room ID must look like 330286535-177")
        if room in self.rooms:
            return "Already joining/joined " + room
        if len(self.rooms) >= 10:
            raise ValueError("Maximum 10 rooms per process")
        bot = Bot(Radio(), room, self.permissions, self.authentication)
        task = asyncio.create_task(self.serve(bot), name="room-" + room)
        self.rooms[room] = (bot, task)
        return "Join requested for " + room + "; use rooms to check readiness"

    async def serve(self, bot):
        async def audio():
            await bot.authenticated.wait()
            await bot.radio.run(bot.say)
        tasks = [asyncio.create_task(coro) for coro in
                 (bot.run(), bot.command_worker(), bot.social.run(), audio())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as error:
            bot.status = "failed"
            bot.status_message = "Room task failed: " + type(error).__name__
            LOG.error("Room %s failed: %s", bot.room, type(error).__name__)
        finally:
            # Remove presence before closing the authenticated HTTP session.
            if bot.session and not bot.session.closed and hasattr(bot, "user_id"):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(bot.api("DELETE", f"/chat/chat-{bot.room}/participants/user-{bot.user_id}"), 10)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bot.radio.broadcast(None)

    async def leave(self, room):
        entry = self.rooms.get(room)
        if entry is None:
            return "Room is not active: " + room
        _, task = entry
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.rooms.pop(room, None)
        return "Stopped room " + room + " (IMVU presence removal requested)"

    def snapshot(self, bot):
        radio = bot.radio
        return {"room": bot.room,
            "status": "connected" if bot.connected and bot.chat_ready else "transport_connected" if bot.connected else bot.status,
            "message": bot.status_message, "chat_ready": bot.chat_ready and bot.connected,
            "participants_recognized": len(bot.members), "chat_diagnostics": bot.diagnostics,
            "ai": bot.social.ai_status, "avatar": bot.appearance,
            "account_presence": bot.account_presence,
            "playing": radio.current["title"] if radio.current else None,
            "queued": radio.queue.qsize(), "listeners": len(radio.listeners),
            "audio_bytes": radio.bytes_sent, "audio_error": radio.last_error,
            "encoder_starts": radio.encoder_starts, "track_starts": radio.track_starts,
            "stream_path": f"/rooms/{bot.room}/stream.mp3"}

    async def health(self, request):
        rooms = [self.snapshot(bot) for bot, _ in self.rooms.values()]
        default = next((room for room in rooms if room["room"] == self.default_room), {"status": "not_joined"})
        return web.json_response({**default, "rooms": rooms})

    async def stream(self, request):
        room = request.match_info.get("room", self.default_room)
        entry = self.rooms.get(room)
        if entry is None or entry[1].done():
            raise web.HTTPNotFound(text="Room is not active")
        return await entry[0].radio.stream(request)

    async def command(self, line):
        parts = line.strip().split()
        if not parts:
            return "Commands: rooms | join ROOM_ID | leave ROOM_ID | help"
        if parts == ["rooms"]:
            return "\n".join(f"{bot.room}: {self.snapshot(bot)['status']} | listeners={len(bot.radio.listeners)}" for bot, _ in self.rooms.values()) or "No active rooms"
        if len(parts) == 2 and parts[0] == "join":
            return await self.join(parts[1])
        if len(parts) == 2 and parts[0] == "leave":
            return await self.leave(parts[1])
        return "Commands: rooms | join ROOM_ID | leave ROOM_ID | help"

    async def terminal(self):
        LOG.info("Terminal controls: rooms | join ROOM_ID | leave ROOM_ID | help")
        transport = None
        if os.name == "nt":
            if not sys.stdin.isatty():
                LOG.warning("Windows terminal controls require an interactive console")
                return
            import msvcrt
            async def readline():
                chars = []
                while True:
                    if not msvcrt.kbhit():
                        await asyncio.sleep(0.05)
                        continue
                    char = msvcrt.getwch()
                    if char in ("\x00", "\xe0"):
                        msvcrt.getwch()
                    elif char in ("\r", "\n"):
                        print(flush=True)
                        return "".join(chars) + "\n"
                    elif char == "\x03":
                        return ""
                    elif char == "\b":
                        if chars:
                            chars.pop()
                            print("\b \b", end="", flush=True)
                    else:
                        chars.append(char)
                        print(char, end="", flush=True)
        else:
            # Docker normally supplies /dev/null, which epoll cannot watch.
            if not sys.stdin.isatty() and not stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode):
                LOG.info("stdin unavailable; use python botctl.py from the container shell")
                return
            reader = asyncio.StreamReader()
            try:
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
            except (OSError, ValueError):
                LOG.warning("stdin is not an interactive pipe/console; controls disabled")
                return
            async def readline():
                return (await reader.readline()).decode(errors="replace")
        try:
            while True:
                line = await readline()
                if not line:
                    LOG.info("stdin closed; terminal controls disabled, bot remains running")
                    return
                try:
                    print(await self.command(line), flush=True)
                except ValueError as error:
                    print(str(error), flush=True)
        finally:
            if transport:
                transport.close()

    async def close(self):
        for room in list(self.rooms):
            await self.leave(room)
