"""Private owner-only Telegram room panels. Every button carries its room."""
import asyncio
import secrets
import time

import aiohttp


class TelegramControls:
    def __init__(self, bridge):
        self.bridge = bridge
        self.manager = bridge.manager
        self.prompts = {}
        self.choices = {}

    @staticmethod
    def button(label, room, action, value=""):
        return {"text": label, "callback_data": "rc|" + room + "|" + action + "|" + value}

    async def send(self, text, rows=None, **kwargs):
        if rows is not None:
            kwargs["reply_markup"] = {"inline_keyboard": rows}
        return await self.bridge.call("sendMessage", chat_id=self.bridge.owner, text=text[:3900],
            link_preview_options={"is_disabled": True}, **kwargs)

    def expire(self):
        now = time.monotonic()
        self.prompts = {k: v for k, v in self.prompts.items() if v[0] > now}
        self.choices = {k: v for k, v in self.choices.items() if v[0] > now}

    async def rooms(self):
        self.prompts.clear()
        rows = []
        for room, (bot, _) in list(self.manager.rooms.items()):
            await self.bridge.resolve_room_name(room)
            title = self.bridge.room_header(room)[13:-1]
            if title == "Unknown room":
                title = getattr(bot, "room_name", room)
            rows.append([self.button(title[:55], room, "open")])
        await self.send("Choose a room to manage its settings and music. Owner only.", rows)

    async def panel(self, room):
        bot = self.manager.rooms[room][0]
        await self.bridge.resolve_room_name(room)
        prefs = self.manager.settings.get(room)
        language = self.manager.room_languages.get(room, "auto")
        status = "connected" if bot.connected and bot.chat_ready else "reconnecting"
        welcome = prefs["welcome"] or "Existing default greeting"
        rows = [
            [self.button("Quiet ON" + (" ✓" if prefs["quiet"] else ""), room, "quiet", "on"),
             self.button("Quiet OFF" + (" ✓" if not prefs["quiet"] else ""), room, "quiet", "off")],
            [self.button("Language: " + language, room, "languages")],
            [self.button("Welcomes ON", room, "welcomes", "on"), self.button("Welcomes OFF", room, "welcomes", "off")],
            [self.button("Set welcome", room, "welcome"), self.button("Restore default welcome", room, "resetwelcome")],
            [self.button("Music controls", room, "music"), self.button("All rooms", "-", "rooms")]]
        await self.send(self.bridge.room_header(room) + "\nStatus: " + status +
            "\nQuiet: " + ("ON" if prefs["quiet"] else "OFF") +
            "\nQuiet ON: chat replies to mentions and commands only. Welcomes use their own ON/OFF switch. Logs and music stay on." +
            "\nLanguage: " + language + "\nWelcomes: " + ("ON" if prefs["welcomes"] else "OFF") +
            "\nWelcome: " + welcome + "\nSettings survive restarts.", rows)

    async def music(self, room):
        radio = self.manager.rooms[room][0].radio
        titles = [str(t["title"])[:180] for t in list(radio.queue._queue)]
        text = self.bridge.room_header(room) + "\nNow playing: " + (
            str(radio.current["title"])[:180] if radio.current else "Nothing")
        text += "\nQueue:\n" + ("\n".join(str(i) + ". " + t for i, t in enumerate(titles, 1)) or "Empty")
        text += "\nListeners: " + str(len(radio.listeners))
        await self.send(text, [
            [self.button("Search / add song", room, "play"), self.button("Refresh queue", room, "music")],
            [self.button("Skip track", room, "skip"), self.button("Stop + clear queue", room, "stop")],
            [self.button("Room settings", room, "open")]])

    async def prompt(self, room, action, text):
        self.expire()
        self.prompts.clear()  # One explicit pending input; room never inferred from chat history.
        result = await self.send(self.bridge.room_header(room) + "\n" + text +
            "\nReply directly to this message within 10 minutes. /cancel to cancel.",
            reply_markup={"force_reply": True, "selective": True})
        self.prompts[str(result["message_id"])] = (time.monotonic() + 600, room, action)

    async def callback(self, query):
        sender = query.get("from", {})
        message = query.get("message", {})
        chat = message.get("chat", {})
        allowed = (type(sender.get("id")) is int and sender["id"] == self.bridge.owner
            and not sender.get("is_bot") and chat.get("id") == self.bridge.owner
            and chat.get("type") == "private" and message.get("from", {}).get("id") == self.bridge.bot_id)
        await self.bridge.call("answerCallbackQuery", callback_query_id=query["id"],
            text="" if allowed else "Owner only.")
        if not allowed:
            return
        parts = str(query.get("data", "")).split("|")
        if len(parts) != 4 or parts[0] != "rc":
            return
        _, room, action, value = parts
        self.expire()
        if action == "rooms":
            await self.rooms()
            return
        if room not in self.manager.rooms:
            await self.send("That room is no longer connected. Use /rooms.")
            return
        bot = self.manager.rooms[room][0]
        try:
            if action == "open":
                await self.panel(room)
            elif action in {"quiet", "welcomes"} and value in {"on", "off"}:
                await self.manager.settings.set(room, action, value == "on")
                await self.panel(room)
            elif action == "resetwelcome":
                self.prompts = {k: v for k, v in self.prompts.items() if v[1:] != (room, "welcome")}
                await self.manager.settings.update(room, {"welcome": "", "welcomes": True})
                await self.panel(room)
            elif action == "welcome":
                await self.prompt(room, "welcome", "Send a welcome (max 300 characters). Use {name} for the visitor and {room} for the room name.")
            elif action == "languages":
                await self.send(self.bridge.room_header(room) + "\nChoose this room's output language.", [
                    [self.button("Automatic", room, "language", "auto"), self.button("English", room, "language", "en")],
                    [self.button("Egyptian Arabic", room, "language", "ar-eg"), self.button("Russian", room, "language", "ru")],
                    [self.button("Other language", room, "otherlanguage"), self.button("Back", room, "open")]])
            elif action == "language":
                await self.manager.set_language(bot, value)
                await self.panel(room)
            elif action == "otherlanguage":
                await self.prompt(room, "language", "Send a language name or ISO code, e.g. French, de, or ar-eg.")
            elif action == "music":
                await self.music(room)
            elif action == "play":
                await self.prompt(room, "play", "Send a song/artist to search, or an HTTPS YouTube/SoundCloud link to queue directly.")
            elif action in {"skip", "stop", "pick"}:
                if not bot.connected or not bot.chat_ready:
                    raise ValueError("Room is reconnecting; try again once connected.")
                if action == "pick":
                    choice = self.choices.pop(value, None)
                    if not choice or choice[1] != room:
                        raise ValueError("Song choice expired or already used. Search again.")
                    try:
                        await self.add_track(room, choice[2]["url"])
                    except RuntimeError as error:
                        if getattr(error, "reason", None) != "youtube_blocked":
                            raise
                        await self.search(room, choice[2].get("search_query", choice[2]["title"]), soundcloud=True)
                else:
                    if action == "stop":
                        while not bot.radio.queue.empty():
                            bot.radio.queue.get_nowait()
                            bot.radio.queue.task_done()
                    bot.radio.interrupt()
                    await self.send(self.bridge.room_header(room) + ("\nMusic stopped; queue cleared." if action == "stop" else "\nSkipping current track."))
                await self.music(room)
        except (ValueError, RuntimeError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
            await self.send("Could not apply that action. Check the room connection and try again. Saved settings remain active.")

    async def add_track(self, room, query):
        bot = self.manager.rooms[room][0]
        if not bot.connected or not bot.chat_ready:
            raise ValueError("Room is reconnecting.")
        if bot.radio.queue.full():
            raise ValueError("Music queue is full.")
        track = await bot.radio.lookup(query)
        if self.manager.rooms.get(room, (None,))[0] is not bot or not bot.connected or not bot.chat_ready:
            raise ValueError("Room disconnected while loading the song.")
        if bot.radio.queue.full():
            raise ValueError("Music queue is full.")
        from permissions import OWNER_ID
        track["requester_id"] = OWNER_ID
        bot.radio.queue.put_nowait(track)
        if bot.radio.idle:
            bot.radio.interrupt()
        await self.send(self.bridge.room_header(room) + "\nQueued: " + str(track["title"])[:180])

    async def search(self, room, query, soundcloud=False):
        radio = self.manager.rooms[room][0].radio
        await self.send(self.bridge.room_header(room) + "\nSearching for songs…")
        choices = await (radio.search_soundcloud(query) if soundcloud else radio.search(query))
        self.expire()
        rows = []
        for choice in choices[:10]:
            key = secrets.token_hex(6)
            self.choices[key] = (time.monotonic() + 600, room, choice)
            rows.append([self.button(str(choice["title"])[:60], room, "pick", key)])
        while len(self.choices) > 100:
            self.choices.pop(next(iter(self.choices)))
        await self.send(self.bridge.room_header(room) + "\nChoose a song to add" + (" (SoundCloud alternatives)" if soundcloud else "") + ":", rows)

    async def message(self, message, command, text):
        # Called only after TelegramBridge's private-owner authorization check.
        self.expire()
        if command in {"/rooms", "/control", "/settings"}:
            await self.rooms()
            return True
        if command == "/cancel":
            self.prompts.clear()
            await self.send("Cancelled. Use /rooms to choose a room.")
            return True
        quoted = message.get("reply_to_message", {})
        pending = self.prompts.get(str(quoted.get("message_id", "")))
        if not pending or quoted.get("from", {}).get("id") != self.bridge.bot_id:
            return False
        _, room, action = pending
        if room not in self.manager.rooms:
            await self.send("That room is no longer active. Use /rooms.")
            return True
        if not text or text.startswith("/"):
            await self.send("Reply with the requested value, or /cancel.")
            return True
        try:
            if action == "welcome":
                await self.manager.settings.update(room, {"welcome": text, "welcomes": True})
                await self.panel(room)
            elif action == "language":
                await self.manager.set_language(self.manager.rooms[room][0], text)
                await self.panel(room)
            elif action == "play":
                radio = self.manager.rooms[room][0].radio
                target = radio.lookup_target(text)
                if target.startswith("ytsearch1:"):
                    await self.search(room, text)
                else:
                    await self.add_track(room, text)
            self.prompts.pop(str(quoted["message_id"]), None)
        except (ValueError, RuntimeError) as error:
            await self.send(str(error)[:400] + "\nReply again or /cancel.")
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError):
            await self.send("Could not complete that action. Reply again to retry, or /cancel.")
        return True
