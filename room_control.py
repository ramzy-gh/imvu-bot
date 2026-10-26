"""Room lifecycle and private local terminal control (never an HTTP admin API)."""
import asyncio
import contextlib
import logging
import os
import re
import sys
import stat
from urllib.parse import urlparse

from aiohttp import web
from yarl import URL

from bot import API, Bot, Radio
from permissions import Permissions
from state_store import StateStore

LOG = logging.getLogger("ramsei.rooms")


def room_id(value):
    value = value.strip()
    if len(value) > 300:
        raise ValueError("Use a room ID or an IMVU room link.")
    if value.startswith("https://"):
        parsed = urlparse(value)
        if parsed.netloc not in {"www.imvu.com", "imvu.com"}:
            raise ValueError("Use an HTTPS IMVU room link, not another website.")
        match = re.fullmatch(r"/next/chat/room-([1-9]\d*-\d+)/?", parsed.path)
        if not match:
            raise ValueError("Use a room ID such as 47377071-5395 or its IMVU room link.")
        value = match[1]
    if not re.fullmatch(r"[1-9]\d*-\d+", value):
        raise ValueError("Room ID must look like 47377071-5395")
    return value


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
        self.default_room = room_id(os.getenv("IMVU_ROOM_ID", "330286535-177"))
        self.room_lock = asyncio.Lock()
        self.pending = set()
        self.closing = False
        self.room_store = StateStore("rooms", os.getenv("ROOMS_PATH", str(self.permissions.path.with_name("rooms.json"))))
        try:
            saved = self.room_store.load()
            configured = os.getenv("IMVU_ROOM_IDS", "").strip()
            values = saved["rooms"] if saved is not None else [r.strip() for r in configured.split(",") if r.strip()]
            if not isinstance(values, list) or any(not isinstance(r, str) for r in values):
                raise ValueError("Invalid room list")
            self.desired_rooms = {room_id(r) for r in values} | {self.default_room}
            if len(self.desired_rooms) > 10:
                raise ValueError("Maximum 10 rooms")
        except (OSError, ValueError, TypeError, KeyError):
            raise RuntimeError("Cannot load saved rooms; repair rooms storage before starting.") from None

    async def save_rooms(self, rooms):
        try:
            await asyncio.to_thread(self.room_store.save, {"version": 1, "rooms": sorted(rooms)})
        except OSError:
            raise ValueError("Could not save room selection. No room change was applied; check persistent storage.") from None

    async def start(self):
        await self.save_rooms(self.desired_rooms)
        for room in [self.default_room, *sorted(self.desired_rooms - {self.default_room})]:
            LOG.info("%s", await self.join(room))

    async def join(self, room):
        room = room_id(room)
        async with self.room_lock:
            if self.closing:
                raise ValueError("Bot is shutting down; try again after restart.")
            if room in self.rooms and not self.rooms[room][1].done():
                return "Already joining/joined " + room
            desired = self.desired_rooms | {room}
            if len(desired) > 10:
                raise ValueError("Maximum 10 rooms including the main room. Leave an extra room first.")
            await self.save_rooms(desired)
            self.desired_rooms = desired
            bot = Bot(Radio(), room, self.permissions, self.authentication, room_manager=self)
            task = asyncio.create_task(self.serve(bot), name="room-" + room)
            self.rooms[room] = (bot, task)
            return "Join requested for " + room + "; use !rooms to check readiness."

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
        room = room_id(room)
        async with self.room_lock:
            if room == self.default_room:
                raise ValueError("Main room " + self.default_room + " is protected and always stays connected.")
            if room not in self.rooms and room not in self.desired_rooms:
                return "Room is not active: " + room
            desired = self.desired_rooms - {room}
            await self.save_rooms(desired)
            self.desired_rooms = desired
            await self.stop_room(room)
            return "Stopped room " + room + " (IMVU presence removal requested)"

    async def stop_room(self, room):
        entry = self.rooms.get(room)
        if entry is None:
            return
        _, task = entry
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.rooms.pop(room, None)

    async def chat_command(self, source, uid, verified, cmd, query):
        error = self.permissions.authorization_error(uid, verified, cmd)
        if error:
            await source.say(error)
            return
        if str(uid) not in source.members or not source.connected or not source.chat_ready:
            await source.say("You must be present in this connected room to use room controls.")
            return
        try:
            if cmd == "!mainroom":
                await source.say("Main room: " + self.default_room + " (protected; always connected).")
            elif cmd == "!rooms":
                await source.say("Rooms: " + " | ".join(
                    ("[MAIN] " if bot.room == self.default_room else "") + bot.room + ": " + self.snapshot(bot)["status"]
                    for bot, _ in self.rooms.values()))
            elif cmd == "!join":
                if not query:
                    await source.say("Usage: !join ROOM_ID or !join https://www.imvu.com/next/chat/room-OWNER-NUMBER/")
                    return
                await source.say(await self.join(query))
            elif cmd == "!leave":
                target = room_id(query) if query and query.lower() != "here" else source.room
                if target == self.default_room:
                    raise ValueError("Main room " + self.default_room + " is protected and always stays connected.")
                # A separate task must leave the source room: awaiting it here
                # would cancel this command worker while it waits on itself.
                await source.say("Leave requested for " + target + ". Main room remains connected.")
                async def finish_leave():
                    try:
                        result = await self.leave(target)
                        if source.room != target:
                            await source.say(result)
                    except ValueError as error:
                        await source.say(str(error))
                task = asyncio.create_task(finish_leave())
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
        except ValueError as error:
            await source.say(str(error))

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
            "listener_resyncs": radio.listener_resyncs,
            "stream_path": f"/rooms/{bot.room}/stream.mp3"}

    async def health(self, request):
        rooms = [self.snapshot(bot) for bot, _ in self.rooms.values()]
        default = next((room for room in rooms if room["room"] == self.default_room), {"status": "not_joined"})
        return web.json_response({**default, "main_room": self.default_room, "rooms": rooms})

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
        self.closing = True
        for task in tuple(self.pending):
            task.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
        for room in list(self.rooms):
            await self.stop_room(room)
