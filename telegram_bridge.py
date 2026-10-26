"""Owner-controlled Telegram relay with revocable read-only viewers."""
import asyncio
import contextlib
import logging
import math
import os
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from state_store import StateStore

LOG = logging.getLogger("ramsei.telegram")
ROOM = re.compile(r"[1-9]\d*-\d+")
HELP = ("RAMSEI room bridge\nReply to a forwarded message to speak in that room.\n"
        "/rooms — list connected rooms\n/say ROOM_ID message — send to a specific room\n"
        "/addviewer TELEGRAM_ID — grant read-only logs for all rooms\n"
        "/removeviewer TELEGRAM_ID — revoke access\n/viewers — list viewers\n"
        "Replies appear as normal messages sent by RAMSEI. Maximum 430 characters.")


class TelegramError(RuntimeError):
    def __init__(self, code=0, delay=5):
        super().__init__("Telegram request failed")  # Never include token-bearing URLs.
        self.code, self.delay = code, min(60, max(1, delay))


class TelegramBridge:
    def __init__(self, manager):
        self.manager = manager
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
        self.enabled = bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{25,}", self.token)
                            and re.fullmatch(r"[1-9]\d*", raw_owner))
        self.owner = int(raw_owner) if raw_owner.isdigit() else 0
        self.bot_id = int(self.token.split(":", 1)[0]) if self.enabled else 0
        self.store = StateStore("telegram-bridge", os.getenv("TELEGRAM_STATE_PATH",
                               str(manager.permissions.path.with_name("telegram-bridge.json"))))
        self.session = self.task = None
        self.state = "disabled" if not self.token else "owner_not_configured"
        self.offset = None
        self.sequence = 0
        self.outbox = []
        self.routes = OrderedDict()
        self.names = OrderedDict()
        self.viewers = {}
        self.viewer_queues = {}
        self.viewer_tasks = {}
        self.viewer_locks = {}
        self.requests = OrderedDict()
        self.wake = asyncio.Event()
        self.save_lock = asyncio.Lock()
        self.dropped = 0
        try:
            self.tz = ZoneInfo(os.getenv("TELEGRAM_TIMEZONE", "Asia/Yekaterinburg"))
        except ZoneInfoNotFoundError:
            self.tz = timezone.utc

    async def persist(self):
        async with self.save_lock:
            payload = {"owner": self.owner, "bot": self.bot_id, "offset": self.offset,
                       "sequence": self.sequence, "outbox": list(self.outbox), "routes": dict(self.routes),
                       "viewers": {uid: dict(info) for uid, info in self.viewers.items()}}
            # Cancellation must not release the lock while its file/DB write is
            # still running in a thread (shutdown saves use the same temp file).
            write = asyncio.create_task(asyncio.to_thread(self.store.save, payload))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                await write
                raise

    async def start(self):
        if not self.enabled:
            return
        try:
            saved = await asyncio.to_thread(self.store.load)
            if saved is not None:
                if saved.get("owner") != self.owner or saved.get("bot") != self.bot_id:
                    raise ValueError("Telegram owner or bot changed")
                self.offset = saved.get("offset")
                if self.offset is not None and (type(self.offset) is not int or self.offset < 0):
                    raise ValueError("Invalid Telegram update cursor")
                self.sequence = int(saved.get("sequence", 0))
                self.outbox = saved.get("outbox", [])
                if not isinstance(self.outbox, list) or len(self.outbox) > 500:
                    raise ValueError("Invalid Telegram outbox")
                for event in self.outbox:
                    if (not isinstance(event, dict) or not ROOM.fullmatch(str(event.get("room", "")))
                            or not isinstance(event.get("text"), str) or len(event["text"]) > 600
                            or type(event.get("id")) is not int or event["id"] <= 0
                            or not isinstance(event.get("at"), (int, float)) or not math.isfinite(event["at"])
                            or not str(event.get("uid", "")).isdigit()):
                        raise ValueError("Invalid Telegram outbox entry")
                if len({event["id"] for event in self.outbox}) != len(self.outbox) or self.sequence < max(
                        (event["id"] for event in self.outbox), default=0):
                    raise ValueError("Invalid Telegram outbox sequence")
                self.routes = OrderedDict((str(k), v) for k, v in saved.get("routes", {}).items()
                                          if str(k).isdigit() and isinstance(v, str) and ROOM.fullmatch(v))
                while len(self.routes) > 2000:
                    self.routes.popitem(last=False)
                viewers = saved.get("viewers", {})
                if not isinstance(viewers, dict) or len(viewers) > 10:
                    raise ValueError("Invalid viewer list")
                for uid, info in viewers.items():
                    if (not isinstance(uid, str) or not re.fullmatch(r"[1-9]\d*", uid) or int(uid) == self.owner
                            or not isinstance(info, dict) or type(info.get("since")) is not int
                            or info["since"] < 1 or not isinstance(info.get("label"), str)
                            or len(info["label"]) > 80 or not isinstance(info.get("generation"), str)
                            or not re.fullmatch(r"[a-f0-9]{24}", info["generation"])):
                        raise ValueError("Invalid viewer entry")
                self.viewers = viewers
            await self.persist()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.enabled = False
            self.state = "storage_error"
            LOG.error("Telegram bridge disabled: repair its private state storage.")
            return
        self.state = "starting"
        self.task = asyncio.create_task(self.run(), name="telegram-bridge")

    def record(self, room, uid, text, username=None):
        if not self.enabled or not ROOM.fullmatch(str(room)) or not str(uid).isdigit():
            return
        if not isinstance(text, str) or not text.strip() or text.lstrip().startswith("*"):
            return
        if len(self.outbox) >= 500:
            self.dropped += 1
            LOG.warning("Telegram outbox full; message not retained.")
            return
        self.sequence += 1
        self.outbox.append({"id": self.sequence, "room": room, "uid": str(uid), "text": text[:600],
                            "at": time.time(), "name": username if isinstance(username, str)
                            and re.fullmatch(r"[A-Za-z0-9_]{1,40}", username) else None})
        self.wake.set()

    async def call(self, method, **payload):
        try:
            async with self.session.post("https://api.telegram.org/bot" + self.token + "/" + method,
                                         json=payload, timeout=aiohttp.ClientTimeout(total=40)) as response:
                data = await response.json(content_type=None)
                if response.status != 200 or not data.get("ok"):
                    raise TelegramError(data.get("error_code", response.status),
                                        data.get("parameters", {}).get("retry_after", 5))
                return data["result"]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
            raise TelegramError() from None

    async def notify(self, text):
        return await self.call("sendMessage", chat_id=self.owner, text=text,
                               link_preview_options={"is_disabled": True})

    async def tell(self, uid, text):
        return await self.call("sendMessage", chat_id=int(uid), text=text,
                               link_preview_options={"is_disabled": True})

    def fanout(self, room, formatted):
        # The event sequence prevents newly approved viewers from receiving
        # older room messages that happen to remain in the owner's outbox.
        for uid, info in list(self.viewers.items()):
            lines = [line for event_id, line in formatted if event_id >= info["since"]]
            if not lines:
                continue
            if uid not in self.viewer_tasks or self.viewer_tasks[uid].done():
                queue = asyncio.Queue(maxsize=100)
                self.viewer_queues[uid] = queue
                self.viewer_tasks[uid] = asyncio.create_task(
                    self.deliver_viewer(uid, info["generation"], queue), name="telegram-viewer")
            queue = self.viewer_queues[uid]
            if queue.full():
                queue.get_nowait()  # A slow viewer never blocks the owner.
            queue.put_nowait("[IMVU room " + room + "]\n" + "\n".join(lines))

    async def deliver_viewer(self, uid, generation, queue):
        lock = self.viewer_locks.setdefault(uid, asyncio.Lock())
        while True:
            text = await queue.get()
            try:
                async with lock:
                    current = self.viewers.get(uid)
                    if not current or current["generation"] != generation:
                        return
                    await self.call("sendMessage", chat_id=int(uid), text=text,
                                    disable_notification=True, link_preview_options={"is_disabled": True})
            except TelegramError as error:
                # Viewer delivery is best effort and independent of owner logs.
                await asyncio.sleep(error.delay)
            await asyncio.sleep(1.05)

    async def viewer_command(self, command, text):
        if command == "/viewers":
            rows = [uid + " — " + info["label"] for uid, info in sorted(self.viewers.items())]
            await self.notify("Read-only viewers (all connected rooms):\n" + ("\n".join(rows) or "none"))
            return
        parts = text.split()
        if len(parts) != 2 or not re.fullmatch(r"[1-9]\d*", parts[1]):
            await self.notify("Use " + command + " TELEGRAM_ID. Ask your friend to press Start on this bot to get their ID.")
            return
        uid = parts[1]
        if int(uid) == self.owner:
            await self.notify("Your owner access is permanent. These commands only manage read-only viewers.")
            return
        lock = self.viewer_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            if command == "/removeviewer":
                previous = self.viewers.pop(uid, None)
                try:
                    await self.persist()
                except OSError:
                    if previous is not None:
                        self.viewers[uid] = previous
                    await self.notify("Could not save this change. Viewer access was not changed.")
                    return
                task = self.viewer_tasks.pop(uid, None)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.viewer_queues.pop(uid, None)
                await self.notify("Removed viewer " + uid + ". No further logs will be sent. Previously delivered messages remain in their Telegram.")
                return
            if uid in self.viewers:
                await self.notify("That account already has read-only access.")
                return
            if len(self.viewers) >= 10:
                await self.notify("Maximum 10 viewers. Remove one before adding another.")
                return
            try:
                chat = await self.call("getChat", chat_id=int(uid))
                if chat.get("id") != int(uid) or chat.get("type") != "private":
                    raise TelegramError()
            except TelegramError:
                await self.notify("I can't reach that private account. Ask your friend to open this bot and press Start first, then use their numeric Telegram ID.")
                return
            label = "@" + chat["username"] if isinstance(chat.get("username"), str) and re.fullmatch(
                r"[A-Za-z0-9_]{1,40}", chat["username"]) else " ".join(str(chat.get("first_name") or "Telegram account").split())[:80]
            self.viewers[uid] = {"since": self.sequence + 1, "generation": secrets.token_hex(12), "label": label}
            try:
                await self.persist()
            except OSError:
                self.viewers.pop(uid, None)
                await self.notify("Could not save this change. Viewer access was not granted.")
                return
        with contextlib.suppress(TelegramError):
            await self.tell(uid, "You have read-only access to RAMSEI's live room logs. You cannot send room replies or manage access.")
        await self.notify("Added " + label + " (" + uid + ") as a read-only viewer for ALL connected rooms. Remove with /removeviewer " + uid)

    async def non_owner(self, uid, command, sender):
        if command not in {"/start", "/help", "/id"}:
            if str(uid) in self.viewers:
                await self.tell(uid, "Read-only access: replies, room commands and access management are owner-only.")
            return
        if str(uid) in self.viewers:
            await self.tell(uid, "You have read-only access to room logs. Your Telegram ID: " + str(uid))
            return
        await self.tell(uid, "Your Telegram ID: " + str(uid) + ". Ask the owner to type /addviewer " + str(uid) + ". No room logs are shared until they approve you.")
        if command == "/start" and time.monotonic() - self.requests.get(uid, -10000) >= 3600:
            self.requests[uid] = time.monotonic()
            self.requests.move_to_end(uid)
            while len(self.requests) > 100:
                self.requests.popitem(last=False)
            name = sender.get("username")
            label = "@" + name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", name) else "Telegram account"
            await self.notify("Read-only access request from " + label + " (ID " + str(uid) + ").\nTo share live logs from ALL connected rooms, type /addviewer " + str(uid) + ".\nNo access has been granted yet.")

    async def username(self, event):
        if event.get("name"):
            return "@" + event["name"]
        uid = event["uid"]
        cached = self.names.get(uid)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        name = "IMVU account " + uid
        entry = self.manager.rooms.get(event["room"])
        if entry and entry[0].session and not entry[0].session.closed:
            try:
                path = "/user/user-" + uid
                data = await asyncio.wait_for(entry[0].api("GET", path), 3)
                actual = data.get("denormalized", {}).get("https://api.imvu.com" + path, {}).get("data", {}).get("username")
                if isinstance(actual, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", actual):
                    name = "@" + actual
            except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
        self.names[uid] = time.monotonic(), name
        self.names.move_to_end(uid)
        while len(self.names) > 1000:
            self.names.popitem(last=False)
        return name

    async def deliver(self):
        while True:
            if not self.outbox:
                self.wake.clear()
                await self.wake.wait()
            await asyncio.sleep(.75)  # Batch a short burst from one room.
            try:
                await self.persist()
                room = self.outbox[0]["room"]
                lines, selected, formatted = ["[IMVU room " + room + "]"], [], []
                for event in list(self.outbox):
                    if event["room"] != room:
                        continue
                    stamp = datetime.fromtimestamp(event["at"], self.tz).strftime("%H:%M:%S %Z")
                    line = stamp + " " + await self.username(event) + ": " + event["text"]
                    if len("\n".join(lines)) + len(line) + 1 > 3500:
                        break
                    lines.append(line)
                    selected.append(event["id"])
                    formatted.append((event["id"], line))
                    if len(selected) >= 8:
                        break
                sent = await self.call("sendMessage", chat_id=self.owner, text="\n".join(lines),
                                       disable_notification=True, link_preview_options={"is_disabled": True})
                message_id = sent["message_id"]
                if type(message_id) is not int:
                    raise TelegramError()
                self.routes[str(message_id)] = room
                while len(self.routes) > 2000:
                    self.routes.popitem(last=False)
                ids = frozenset(selected)
                self.outbox = [event for event in self.outbox if event["id"] not in ids]
                await self.persist()
                self.fanout(room, formatted)
                self.state = "connected"
                await asyncio.sleep(1.05)
            except (TelegramError, OSError) as error:
                self.state = "retrying"
                LOG.warning("Telegram forwarding delayed; messages remain in the private outbox.")
                await asyncio.sleep(error.delay if isinstance(error, TelegramError) else 5)

    async def handle_message(self, message):
        sender, chat = message.get("from", {}), message.get("chat", {})
        uid = sender.get("id")
        if (type(uid) is not int or uid <= 0 or sender.get("is_bot") or chat.get("id") != uid
                or chat.get("type") != "private"):
            return
        text = message.get("text", "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if uid != self.owner:
            await self.non_owner(uid, command, sender)
            return
        if command in {"/addviewer", "/removeviewer", "/viewers"}:
            await self.viewer_command(command, text)
            return
        if command in {"/start", "/help"}:
            await self.notify(HELP)
            return
        if command == "/rooms":
            await self.notify("Rooms:\n" + "\n".join(room + ": " +
                ("connected" if bot.connected and bot.chat_ready else "reconnecting")
                for room, (bot, _) in self.manager.rooms.items()))
            return
        if command == "/say":
            parts = text.split(maxsplit=2)
            if len(parts) != 3 or not ROOM.fullmatch(parts[1]):
                await self.notify("Use /say ROOM_ID message. Use /rooms to see room IDs.")
                return
            room, body = parts[1], parts[2].strip()
        else:
            quoted = message.get("reply_to_message", {})
            room = self.routes.get(str(quoted.get("message_id", "")))
            if room is None or quoted.get("from", {}).get("id") != self.bot_id:
                await self.notify("Reply to a forwarded room message, or use /say ROOM_ID message.")
                return
            body = text
        if not body or len(body) > 430 or body.startswith(("*", "/")) or any(ord(c) < 32 and c != "\n" for c in body):
            await self.notify("Send plain chat text, 1–430 characters. System/action commands are not supported.")
            return
        entry = self.manager.rooms.get(room)
        if not entry or not entry[0].connected or not entry[0].chat_ready:
            await self.notify("Room " + room + " is not connected. Nothing was sent.")
            return
        try:
            sent = await entry[0].say(body)
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            sent = False
        await self.notify(("Sent to room " if sent else "Could not send to room ") + room + ".")

    async def poll(self):
        while True:
            try:
                updates = await self.call("getUpdates", offset=self.offset, timeout=25, allowed_updates=["message"])
                for update in updates:
                    update_id = update.get("update_id")
                    if type(update_id) is not int or update_id < (self.offset or 0):
                        continue
                    # Claim before posting to IMVU: a restart must not replay a
                    # previously processed owner reply into a room.
                    self.offset = update_id + 1
                    await self.persist()
                    if isinstance(update.get("message"), dict):
                        await self.handle_message(update["message"])
            except (TelegramError, OSError) as error:
                self.state = "polling_conflict" if isinstance(error, TelegramError) and error.code == 409 else "retrying"
                LOG.warning("Telegram receiving delayed; no credential or message contents logged.")
                await asyncio.sleep(error.delay if isinstance(error, TelegramError) else 5)

    async def run(self):
        while True:
            tasks = []
            try:
                async with aiohttp.ClientSession() as self.session:
                    identity = await self.call("getMe")
                    if identity.get("id") != self.bot_id:
                        raise TelegramError(401)
                    webhook = await self.call("getWebhookInfo")
                    if webhook.get("url"):
                        self.state = "webhook_conflict"
                        await asyncio.sleep(30)
                        continue
                    if self.offset is None:
                        old = await self.call("getUpdates", offset=-1, timeout=0, allowed_updates=["message"])
                        self.offset = max((u["update_id"] + 1 for u in old), default=0)
                        await self.persist()
                    await self.notify(HELP)
                    self.state = "connected"
                    tasks = [asyncio.create_task(self.deliver()), asyncio.create_task(self.poll())]
                    await asyncio.gather(*tasks)
            except (TelegramError, OSError, ValueError, TypeError, KeyError):
                self.state = "retrying"
                LOG.warning("Telegram bridge unavailable; IMVU rooms remain independent.")
                await asyncio.sleep(10)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for task in self.viewer_tasks.values():
            task.cancel()
        await asyncio.gather(*self.viewer_tasks.values(), return_exceptions=True)
        self.viewer_tasks.clear()
        self.viewer_queues.clear()
        if self.enabled:
            with contextlib.suppress(OSError):
                await self.persist()

    def snapshot(self):
        return {"state": self.state, "pending_messages": len(self.outbox), "dropped_messages": self.dropped}
