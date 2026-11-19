"""Owner-only Discord DM relay and controls, independent of Telegram."""
import asyncio
import contextlib
import contextvars
import hashlib
import logging
import math
import os
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone

import aiohttp

from state_store import StateStore

LOG = logging.getLogger("ramsei.discord")
ROOM = re.compile(r"[1-9]\d*-\d+")
SNOWFLAKE = re.compile(r"[1-9]\d{5,24}")
LIMIT = 5000
RESPONSE = contextvars.ContextVar("discord_response", default=None)
HELP = ("RamzyBots Discord bridge\n"
        "Reply to a room log to speak in that IMVU room, or use !say ROOM_ID message.\n"
        "!rooms | !status | !help\n"
        "!quiet ROOM_ID on/off | !welcomes ROOM_ID on/off\n"
        "!welcome ROOM_ID text/default | !language ROOM_ID en/ar-eg/ru/auto\n"
        "!play ROOM_ID song/link | !pick ROOM_ID 1-10\n"
        "!np ROOM_ID | !queue ROOM_ID | !skip ROOM_ID | !stop ROOM_ID\n"
        "!join ROOM_ID | !leave ROOM_ID\n"
        "!imvu ROOM_ID command (for example !addadmin Username)\n"
        "Only the configured Discord owner can control the bot.")


class DiscordError(RuntimeError):
    def __init__(self, code=0, delay=5):
        super().__init__("Discord request failed")
        self.code = code
        self.delay = max(1, float(delay))


def size(text):
    return len(text.encode("utf-16-le")) // 2


class DiscordBridge:
    def __init__(self, manager):
        self.manager = manager
        self.token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        self.owner = os.getenv("DISCORD_OWNER_ID", "").strip()
        self.guild = os.getenv("DISCORD_GUILD_ID", "").strip()
        self.enabled = bool(self.token and (SNOWFLAKE.fullmatch(self.owner) or SNOWFLAKE.fullmatch(self.guild)))
        self.state = "starting" if self.enabled else "disabled"
        if self.token and not self.enabled:
            self.state = "owner_not_configured"
        self.store = StateStore("discord-bridge", os.getenv("DISCORD_STATE_PATH",
            str(manager.permissions.path.with_name("discord-bridge.json"))))
        self.outbox = []
        self.routes = OrderedDict()
        self.cursor = None
        self.bot_id = self.channel = None
        self.sequence = self.dropped = 0
        self.persisted_sequence = -1
        self.session = self.task = self.checkpoint_task = None
        self.wake = asyncio.Event()
        self.save_lock = asyncio.Lock()
        self.http_lock = asyncio.Lock()
        self.not_before = 0
        self.last_overflow = -60
        self.choices = {}
        self.telegram_imported = False
        self.community = None
        self.initializer = None
        self.profiles = OrderedDict()

    async def persist(self):
        async with self.save_lock:
            payload = {"version": 1, "owner": self.owner, "bot": self.bot_id,
                "cursor": self.cursor, "sequence": self.sequence, "outbox": list(self.outbox),
                "routes": dict(self.routes), "dropped": self.dropped, "telegram_imported": self.telegram_imported}
            write = asyncio.create_task(asyncio.to_thread(self.store.save, payload))
            try:
                await asyncio.shield(write)
                self.persisted_sequence = payload["sequence"]
            except asyncio.CancelledError:
                await write
                raise

    async def start(self):
        if not self.enabled:
            return
        if not self.owner:
            self.initializer = asyncio.create_task(self.resolve_owner(), name="discord-setup")
            return
        await self.load_and_start()

    async def resolve_owner(self):
        while self.enabled:
            try:
                async with aiohttp.ClientSession() as self.session:
                    guild = await self.api("GET", "/guilds/" + self.guild)
                    self.owner = str(guild.get("owner_id", ""))
                    if not SNOWFLAKE.fullmatch(self.owner):
                        raise DiscordError(403)
                await self.load_and_start()
                return
            except DiscordError as error:
                self.state = "invalid_token" if error.code == 401 else "guild_unavailable"
                LOG.warning("Discord owner setup delayed: code=%s", error.code)
                if error.code in {401, 403, 404}:
                    return
                await asyncio.sleep(error.delay)

    async def load_and_start(self):
        incoming = list(self.outbox)
        self.outbox = []
        arriving_during_load = self.outbox
        self.sequence = 0
        try:
            saved = await asyncio.to_thread(self.store.load)
            if saved is not None:
                if saved.get("version") != 1 or saved.get("owner") != self.owner:
                    raise ValueError("Discord owner changed")
                self.bot_id, self.cursor = saved.get("bot"), saved.get("cursor")
                for value in (self.bot_id, None if self.cursor == "0" else self.cursor):
                    if value is not None and not SNOWFLAKE.fullmatch(str(value)):
                        raise ValueError("Invalid Discord state")
                self.outbox = saved.get("outbox", [])
                self.sequence = saved.get("sequence", 0)
                self.dropped = saved.get("dropped", 0)
                self.telegram_imported = saved.get("telegram_imported", False)
                if type(self.telegram_imported) is not bool:
                    raise ValueError("Invalid migration state")
                if (not isinstance(self.outbox, list) or len(self.outbox) > LIMIT
                        or type(self.sequence) is not int or self.sequence < 0
                        or type(self.dropped) is not int or self.dropped < 0):
                    raise ValueError("Invalid Discord outbox")
                ids = set()
                for event in self.outbox:
                    if (not isinstance(event, dict) or not ROOM.fullmatch(str(event.get("room", "")))
                            or not str(event.get("uid", "")).isdigit()
                            or not isinstance(event.get("text"), str) or len(event["text"]) > 600
                            or type(event.get("id")) is not int or not 0 < event["id"] <= self.sequence
                            or event["id"] in ids or not isinstance(event.get("at"), (int, float))
                            or not math.isfinite(event["at"])
                            or (event.get("name") is not None and (not isinstance(event["name"], str)
                                or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", event["name"])))
                            or event.get("kind", "chat") not in {"chat", "joined", "left"}):
                        raise ValueError("Invalid Discord event")
                    ids.add(event["id"])
                routes = saved.get("routes", {})
                if not isinstance(routes, dict) or len(routes) > 2000:
                    raise ValueError("Invalid Discord routes")
                if any(not SNOWFLAKE.fullmatch(k) or not isinstance(v, str) or not ROOM.fullmatch(v)
                       for k, v in routes.items()):
                    raise ValueError("Invalid Discord route")
                self.routes = OrderedDict(routes)
            if not self.telegram_imported and os.getenv("CHAT_RELAY", "").lower() == "discord":
                await self.import_telegram()
            # Room messages can arrive while the first owner lookup is in flight.
            incoming.extend(arriving_during_load)
            if self.outbox is arriving_during_load:
                self.outbox = []
            for event in incoming:
                self.sequence += 1
                event["id"] = self.sequence
                self.outbox.append(event)
            if len(self.outbox) > LIMIT:
                self.dropped += len(self.outbox) - LIMIT
                self.outbox = self.outbox[-LIMIT:]
            await self.persist()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.state, self.enabled = "storage_error", False
            LOG.error("Discord bridge disabled: repair its private state storage")
            return
        self.task = asyncio.create_task(self.run(), name="discord-bridge")
        self.checkpoint_task = asyncio.create_task(self.checkpoint(), name="discord-checkpoint")

    async def import_telegram(self):
        """Copy the authorized owner's unsent backlog once; retain the old snapshot for rollback."""
        store = StateStore("telegram-bridge", os.getenv("TELEGRAM_STATE_PATH",
            str(self.manager.permissions.path.with_name("telegram-bridge.json"))))
        try:
            saved = await asyncio.to_thread(store.load)
            if saved is None:
                self.telegram_imported = True
                return
            expected_owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
            expected_bot = os.getenv("TELEGRAM_BOT_TOKEN", "").split(":", 1)[0].strip()
            if not expected_owner or not expected_bot or str(saved.get("owner")) != expected_owner or str(saved.get("bot")) != expected_bot:
                raise ValueError("Telegram identity did not match")
            pending = saved.get("outbox", [])
            if not isinstance(pending, list) or len(pending) > LIMIT:
                raise ValueError("Invalid Telegram backlog")
            copied = []
            for original in pending:
                event = dict(original)
                if (not ROOM.fullmatch(str(event.get("room", ""))) or not str(event.get("uid", "")).isdigit()
                        or not isinstance(event.get("text"), str) or len(event["text"]) > 600
                        or not isinstance(event.get("at"), (int, float)) or not math.isfinite(event["at"])
                        or event.get("kind", "chat") not in {"chat", "joined", "left"}
                        or (event.get("name") is not None and (not isinstance(event["name"], str)
                            or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", event["name"])))):
                    raise ValueError("Invalid Telegram event")
                copied.append(event)
            for event in copied:
                self.sequence += 1
                event["id"] = self.sequence
                self.outbox.append(event)
            self.telegram_imported = True
            LOG.info("Discord migration copied %s pending Telegram events", len(copied))
        except (OSError, ValueError, TypeError, AttributeError):
            LOG.warning("Telegram backlog could not be imported; original state was preserved")

    async def checkpoint(self):
        while True:
            await asyncio.sleep(5)
            if self.sequence != self.persisted_sequence:
                try:
                    await self.persist()
                except OSError:
                    LOG.warning("Discord queue checkpoint delayed; retained in memory")

    def record(self, room, uid, text, username=None):
        if (not self.enabled or not ROOM.fullmatch(str(room)) or not str(uid).isdigit()
                or not isinstance(text, str) or not text.strip() or text.lstrip().startswith("*")):
            return
        if len(self.outbox) >= LIMIT:
            index = next((i for i, e in enumerate(self.outbox) if e.get("kind") in {"joined", "left"}), 0)
            self.outbox.pop(index)
            self.dropped += 1
            if time.monotonic() - self.last_overflow >= 60:
                LOG.warning("Discord outbox at capacity; oldest pending event replaced; dropped=%s", self.dropped)
                self.last_overflow = time.monotonic()
        self.sequence += 1
        text = text.encode("utf-8", errors="replace").decode("utf-8")
        self.outbox.append({"id": self.sequence, "room": str(room), "uid": str(uid),
            "text": text[:600], "at": time.time(), "name": username if isinstance(username, str)
            and re.fullmatch(r"[A-Za-z0-9_]{1,40}", username) else None})
        self.wake.set()

    def record_membership(self, room, uid, action):
        if action not in {"joined", "left"}:
            return
        sequence = self.sequence
        self.record(room, uid, action)
        if self.sequence != sequence:
            self.outbox[-1]["kind"] = action

    async def remove_room(self, room):
        self.outbox = [e for e in self.outbox if e["room"] != room]
        self.routes = OrderedDict((k, v) for k, v in self.routes.items() if v != room)
        self.choices.pop(room, None)
        if self.enabled:
            await self.persist()

    async def api(self, method, path, **kwargs):
        # One shared gate observes Discord's dynamic route and global limits.
        async with self.http_lock:
            await asyncio.sleep(max(0, self.not_before - time.monotonic()))
            try:
                async with self.session.request(method, "https://discord.com/api/v10" + path,
                        headers={"Authorization": "Bot " + self.token,
                                 "User-Agent": "DiscordBot (https://ramzybots.site, 1.0)"},
                        timeout=aiohttp.ClientTimeout(total=25, connect=8), **kwargs) as response:
                    if response.status == 204:
                        return {}
                    data = await response.json(content_type=None)
                    if response.headers.get("X-RateLimit-Remaining") == "0":
                        delay = float(response.headers.get("X-RateLimit-Reset-After", 1))
                        self.not_before = max(self.not_before, time.monotonic() + delay)
                    if response.status == 429:
                        delay = float(data.get("retry_after", response.headers.get("Retry-After", 5)))
                        self.not_before = max(self.not_before, time.monotonic() + delay)
                        raise DiscordError(429, delay)
                    if response.status >= 400:
                        raise DiscordError(response.status, 60 if response.status in {401, 403, 404} else 5)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
                raise DiscordError() from None

    async def send(self, text, *, channel=None, **kwargs):
        # IMVU text must never cause Discord mentions or link-preview fetches.
        response = RESPONSE.get()
        if response is not None and channel is None:
            return await response(text, **kwargs)
        return await self.api("POST", f"/channels/{channel or self.channel}/messages", json={
            "content": text, "allowed_mentions": {"parse": [], "replied_user": False}, "flags": 4, **kwargs})

    def header(self, room):
        entry = self.manager.rooms.get(room)
        name = str(getattr(entry[0], "room_name", room) or room)[:200] if entry else room
        name = name.encode("utf-8", errors="replace").decode("utf-8")
        return "[IMVU room: " + name + " | " + room + "]"

    async def resolve_profiles(self):
        """Bounded, cached public profile lookup; never delay the IMVU receiver."""
        if not self.outbox:
            return
        events = [e for e in self.outbox if e["room"] == self.outbox[0]["room"]][:30]
        now = time.monotonic()
        missing = {}
        for event in events:
            uid = event["uid"]
            cached = self.profiles.get(uid)
            if cached and cached[0] > now:
                event.update(cached[1])
                self.profiles.move_to_end(uid)
            elif uid not in missing:
                missing[uid] = event["room"]
        gate = asyncio.Semaphore(4)
        async def lookup(uid, room):
            result = {}
            entry = self.manager.rooms.get(room)
            if entry and callable(getattr(entry[0], "api", None)):
                try:
                    async with gate:
                        data = await asyncio.wait_for(entry[0].api("GET", "/user/user-" + uid), 4)
                    info = data.get("denormalized", {}).get("https://api.imvu.com/user/user-" + uid, {}).get("data", {})
                    username = info.get("username")
                    if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", username):
                        result["name"] = username
                    nickname = info.get("display_name")
                    if isinstance(nickname, str):
                        # Keep Unicode nicknames but strip formatting/control characters and bound length.
                        import unicodedata
                        result["nickname"] = "".join(c for c in nickname if not unicodedata.category(c).startswith("C"))[:60].strip()
                except (RuntimeError, OSError, ValueError, TypeError, AttributeError, aiohttp.ClientError, asyncio.TimeoutError):
                    pass
            self.profiles[uid] = (time.monotonic() + (3600 if result else 60), result)
            while len(self.profiles) > 2048:
                self.profiles.popitem(last=False)
            for event in events:
                if event["uid"] == uid:
                    event.update(result)
        await asyncio.gather(*(lookup(uid, room) for uid, room in missing.items()))

    def batch(self):
        room = self.outbox[0]["room"]
        lines, selected = [self.header(room)], []
        for event in list(self.outbox):
            if event["room"] != room:
                continue
            stamp = datetime.fromtimestamp(event["at"], timezone.utc).strftime("%H:%M:%S UTC")
            name = "@" + event["name"] if event.get("name") else "IMVU " + event["uid"]
            nickname = event.get("nickname")
            if nickname and nickname.casefold() != str(event.get("name", "")).casefold():
                name = re.sub(r"([\\`*_{}\[\]()<>|~])", r"\\\1", nickname) + " (" + name + ")"
            kind = event.get("kind", "chat")
            line = stamp + " " + name + (" " + kind + " the room." if kind != "chat" else ": " + event["text"])
            if size("\n".join([*lines, line])) > 1900:
                break
            lines.append(line)
            selected.append(event["id"])
            if len(selected) >= 30:
                break
        return room, "\n".join(lines), set(selected)

    async def deliver(self):
        while True:
            if not self.outbox:
                self.wake.clear()
                await self.wake.wait()
            if not self.outbox:
                continue
            await self.resolve_profiles()
            if not self.outbox:
                continue
            await self.persist()
            room, text, ids = self.batch()
            if not ids:
                raise ValueError("Discord batch did not fit")
            if self.community:
                channel = await self.community.room_channel(room)
                nonce = hashlib.sha256((self.store.key + ":" + room + ":" + str(min(ids)) + ":" + str(max(ids))).encode()).hexdigest()[:24]
                sent = await self.send(text, channel=channel, components=self.community.log_buttons(room),
                                       nonce=nonce, enforce_nonce=True)
            else:
                sent = await self.send(text)
            message_id = str(sent.get("id", ""))
            if not SNOWFLAKE.fullmatch(message_id):
                raise DiscordError()
            if any(e["id"] in ids for e in self.outbox):
                self.routes[message_id] = room
                while len(self.routes) > 2000:
                    self.routes.popitem(last=False)
                self.outbox = [e for e in self.outbox if e["id"] not in ids]
                await self.persist()
            self.state = "connected"
            await asyncio.sleep(1)

    async def handle(self, message):
        author = message.get("author", {})
        if (str(author.get("id")) != self.owner or author.get("bot")
                or str(message.get("channel_id")) != self.channel or message.get("guild_id")):
            return
        text = str(message.get("content") or "").strip()
        if not text:
            return
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower()
        if cmd in {"!help", "/help"}:
            await self.send(self.community.help_text() if self.community else HELP)
            return
        if cmd in {"!rooms", "/rooms"}:
            rows = [self.header(room) + (" - connected" if bot.connected and bot.chat_ready else " - reconnecting")
                    for room, (bot, _) in list(self.manager.rooms.items())]
            # Up to 10 rooms, potentially long user-supplied room names.
            for row in rows or ["No rooms are connected."]:
                await self.send(row)
            return
        if cmd == "!status":
            await self.send(f"Discord: {self.state}; pending events: {len(self.outbox)}; dropped: {self.dropped}")
            return
        try:
            if cmd in {"!join", "!leave"} and len(parts) == 2:
                await self.send(await self.manager.command(cmd[1:] + " " + parts[1]))
                return
            if cmd.startswith("!"):
                if len(parts) < 2 or not ROOM.fullmatch(parts[1]):
                    raise ValueError("Include the room ID. Use !help for examples.")
                room, body = parts[1], parts[2] if len(parts) == 3 else ""
            else:
                reference = message.get("message_reference", {})
                room = self.routes.get(str(reference.get("message_id", "")))
                if not room:
                    raise ValueError("Reply directly to a room log, or use !say ROOM_ID message.")
                body = text
                cmd = "!say"
            entry = self.manager.rooms.get(room)
            if not entry:
                raise ValueError("That room is not active. Use !rooms.")
            bot = entry[0]
            if cmd in {"!quiet", "!welcomes"}:
                if body not in {"on", "off"}:
                    raise ValueError("Choose on or off.")
                await self.manager.settings.set(room, cmd[1:], body == "on")
                await self.send(self.header(room) + "\n" + cmd[1:] + ": " + body)
            elif cmd == "!welcome":
                await self.manager.settings.update(room, {"welcome": "" if body == "default" else body, "welcomes": True})
                await self.send(self.header(room) + "\nWelcome updated.")
            elif cmd == "!language":
                language = await self.manager.set_language(bot, body)
                await self.send(self.header(room) + "\nLanguage: " + language)
            elif cmd in {"!play", "!pick", "!np", "!queue", "!skip", "!stop"}:
                await self.music(bot, cmd, body)
            elif cmd == "!imvu":
                if not body.startswith("!") or len(body) > 430 or any(ord(c) < 32 for c in body):
                    raise ValueError("Use !imvu ROOM_ID !command arguments.")
                if not bot.connected or not bot.chat_ready or bot.commands.full():
                    raise ValueError("Room is reconnecting or busy.")
                from permissions import OwnerCommand
                bot.commands.put_nowait(OwnerCommand(room, body))
                await self.send(self.header(room) + "\nOwner command queued; results appear in the room log.")
            elif cmd == "!say":
                if (not body or len(body) > 430 or body.startswith(("*", "/", "!"))
                        or any(ord(c) < 32 and c != "\n" for c in body)):
                    raise ValueError("Use plain chat text, 1-430 characters. Use !imvu for bot commands.")
                if not bot.connected or not bot.chat_ready:
                    raise ValueError("Room is reconnecting; nothing was sent.")
                sent = await bot.say(body)
                await self.send(self.header(room) + ("\nSent." if sent else "\nCould not send."))
            else:
                raise ValueError("Unknown command. Use !help.")
        except ValueError as error:
            await self.send(str(error)[:1500])
        except (RuntimeError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
            await self.send("Could not complete that action. Check the room and try again.")

    async def music(self, bot, command, body):
        room, radio = bot.room, bot.radio
        if not bot.connected or not bot.chat_ready:
            raise ValueError("Room is reconnecting.")
        if command == "!np":
            await self.send(self.header(room) + "\n" + (radio.current["title"] if radio.current else "Nothing playing."))
        elif command == "!queue":
            titles = [str(t["title"])[:150] for t in list(radio.queue._queue)[:10]]
            await self.send(self.header(room) + "\n" + ("\n".join(titles) or "Queue empty."))
        elif command in {"!skip", "!stop"}:
            if command == "!stop":
                while not radio.queue.empty():
                    radio.queue.get_nowait()
                    radio.queue.task_done()
            radio.interrupt()
            await self.send(self.header(room) + ("\nStopped; queue cleared." if command == "!stop" else "\nSkipping."))
        else:
            if command == "!pick":
                choice = self.choices.get(room)
                if not choice or choice[0] < time.monotonic() or not body.isdigit() or not 1 <= int(body) <= len(choice[1]):
                    raise ValueError("Song choice expired or invalid. Use !play ROOM_ID song.")
                query = choice[1][int(body) - 1]["url"]
            else:
                if not body:
                    raise ValueError("Include a song name or link.")
                query = radio.lookup_target(body)
                if query.startswith("ytsearch1:"):
                    choices = (await radio.search(body))[:10]
                    choice_key = secrets.token_hex(4)
                    self.choices[room] = (time.monotonic() + 600, choices, choice_key)
                    rows = [str(i) + ". " + str(c["title"])[:120] for i, c in enumerate(choices, 1)]
                    extra = {}
                    instruction = "\nUse !pick " + room + " NUMBER within 10 minutes."
                    if self.community:
                        from discord_community import button, row
                        buttons = [button(str(i) + ". " + str(c["title"])[:55],
                            "pick|" + room + "|" + str(i) + "|" + choice_key, style=1)
                            for i, c in enumerate(choices, 1)]
                        extra["components"] = [row(*buttons[i:i+5]) for i in range(0, len(buttons), 5)]
                        instruction = "\nChoose a song below within 10 minutes."
                    await self.send(self.header(room) + "\n" + ("\n".join(rows) or "No songs found.") + instruction, **extra)
                    return
            if radio.queue.full():
                raise ValueError("Music queue is full.")
            track = await radio.lookup(query)
            if self.manager.rooms.get(room, (None,))[0] is not bot or not bot.connected or not bot.chat_ready:
                raise ValueError("Room disconnected while loading the song.")
            if radio.queue.full():
                raise ValueError("Music queue is full.")
            from permissions import OWNER_ID
            track["requester_id"] = OWNER_ID
            radio.queue.put_nowait(track)
            self.choices.pop(room, None)
            if radio.idle:
                radio.interrupt()
            await self.send(self.header(room) + "\nQueued: " + str(track["title"])[:180])

    async def poll(self):
        while True:
            params = {"limit": "100", "after": self.cursor} if self.cursor else {"limit": "100"}
            messages = await self.api("GET", f"/channels/{self.channel}/messages", params=params)
            if not isinstance(messages, list):
                raise DiscordError()
            for message in sorted(messages, key=lambda m: int(m["id"])):
                mid = str(message["id"])
                if self.cursor is not None and int(mid) <= int(self.cursor):
                    continue
                # Save before touching IMVU so restarts cannot replay owner commands.
                self.cursor = mid
                await self.persist()
                await self.handle(message)
            await asyncio.sleep(3)

    async def run(self):
        while True:
            tasks = []
            delay = 5
            try:
                async with aiohttp.ClientSession() as self.session:
                    me = await self.api("GET", "/users/@me")
                    bot_id = str(me.get("id", ""))
                    if not me.get("bot") or not SNOWFLAKE.fullmatch(bot_id) or (self.bot_id and self.bot_id != bot_id):
                        self.state = "bot_identity_mismatch"
                        return
                    self.bot_id = bot_id
                    if self.guild:
                        from discord_community import DiscordCommunity
                        self.community = DiscordCommunity(self)
                        await self.community.setup()
                        self.channel = self.community.control
                    else:
                        dm = await self.api("POST", "/users/@me/channels", json={"recipient_id": self.owner})
                        self.channel = str(dm.get("id", ""))
                        if dm.get("type") != 1 or not SNOWFLAKE.fullmatch(self.channel):
                            raise DiscordError(403)
                        recipients = dm.get("recipients", [])
                        if len(recipients) != 1 or str(recipients[0].get("id")) != self.owner:
                            raise DiscordError(403)
                        if self.cursor is None:
                            recent = await self.api("GET", f"/channels/{self.channel}/messages", params={"limit": "1"})
                            self.cursor = str(recent[0]["id"]) if recent else "0"
                    await self.persist()
                    await self.send(self.community.help_text() if self.community else HELP)
                    self.state = "connected"
                    LOG.info("Discord relay connected: mode=%s", "community" if self.community else "private_dm")
                    tasks = [asyncio.create_task(self.deliver())]
                    if self.community:
                        tasks.extend([asyncio.create_task(self.community.gateway()),
                                      asyncio.create_task(self.community.watch_orders())])
                    else:
                        tasks.append(asyncio.create_task(self.poll()))
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        if self.community:
                            await self.community.close()
            except DiscordError as error:
                self.state = "retrying"
                LOG.warning("Discord relay delayed: code=%s pending=%s; no credentials logged", error.code, len(self.outbox))
                if error.code in {401, 403, 404}:
                    self.state = "invalid_token" if error.code == 401 else "dm_unavailable"
                    return  # Correct credentials/access before restarting; avoid invalid-request bans.
                delay = error.delay
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self.state = "retrying"
                LOG.warning("Discord relay interrupted; pending messages remain private")
            await asyncio.sleep(delay)

    async def close(self):
        tasks = [task for task in (self.initializer, self.task, self.checkpoint_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.enabled:
            with contextlib.suppress(OSError):
                await self.persist()

    def snapshot(self):
        return {"state": self.state, "pending_messages": len(self.outbox), "dropped_messages": self.dropped}

