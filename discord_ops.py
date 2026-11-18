"""Durable private Discord operations: alerts, trial reminders, board and payment reviews."""
import hashlib
import json
import time

from discord_bridge import DiscordError, size


def clean(value, limit=180):
    return " ".join(str(value or "").split())[:limit].replace("`", "'")


class DiscordOperations:
    def __init__(self, community):
        self.c = community
        self.portal = community.portal
        self.started = time.time()
        if self.portal:
            self.portal.db.executescript("""
                CREATE TABLE IF NOT EXISTS discord_ops_state (
                    guild TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(guild, key));
                CREATE TABLE IF NOT EXISTS discord_ops_events (
                    guild TEXT NOT NULL, key TEXT NOT NULL, channel TEXT NOT NULL, payload TEXT NOT NULL,
                    message TEXT, PRIMARY KEY(guild, key));
            """)

    def get(self, key, default=None):
        found = self.portal.one("SELECT value FROM discord_ops_state WHERE guild = ? AND key = ?", self.c.guild, key)
        return json.loads(found["value"]) if found else default

    def put(self, key, value):
        self.portal.db.execute("INSERT INTO discord_ops_state VALUES (?, ?, ?) ON CONFLICT(guild, key) DO UPDATE SET value = excluded.value",
                               (self.c.guild, key, json.dumps(value)))

    def queue(self, key, channel, text):
        self.portal.db.execute("INSERT OR IGNORE INTO discord_ops_events(guild, key, channel, payload) VALUES (?, ?, ?, ?)",
            (self.c.guild, key, channel, json.dumps({"content": text, "allowed_mentions": {"parse": []}, "flags": 4})))

    async def flush(self):
        for event in self.portal.db.execute("SELECT * FROM discord_ops_events WHERE guild = ? AND message IS NULL ORDER BY rowid LIMIT 20",
                                             (self.c.guild,)).fetchall():
            payload = json.loads(event["payload"])
            valid = True
            if event["key"].startswith("trial:"):
                _, room, expires, stage = event["key"].split(":")
                access = self.portal.room_access(room)
                valid = bool(access and access["trial_requested"] and not access["unlimited"]
                    and access["expires"] == int(expires)
                    and (int(expires) > time.time() if stage == "24h" else time.time() - 86400 <= int(expires) <= time.time())
                    and self.portal.one("SELECT 1 FROM rooms WHERE id = ? AND status = 'approved'", room)
                    and not self.portal.one("SELECT 1 FROM owner_room_terms WHERE room = ?", room)
                    and not self.portal.one("SELECT 1 FROM vcoin_orders WHERE room = ? AND transfer IS NOT NULL", room))
            if not valid:
                self.portal.db.execute("UPDATE discord_ops_events SET message = 'cancelled' WHERE guild = ? AND key = ?", (self.c.guild, event["key"]))
                continue
            payload.update(nonce=hashlib.sha256((self.c.guild + event["key"]).encode()).hexdigest()[:24], enforce_nonce=True)
            sent = await self.c.api("POST", "/channels/" + event["channel"] + "/messages", json=payload)
            self.portal.db.execute("UPDATE discord_ops_events SET message = ? WHERE guild = ? AND key = ?",
                                   (sent["id"], self.c.guild, event["key"]))

    async def setup(self):
        self.alerts = (await self.c.ensure_channel("room-alerts", parent=self.c.categories["PRIVATE CONTROL"], private=True,
            topic="Room disconnect, access-denied and recovery alerts. Repeated failures are suppressed."))["id"]
        self.reminders = (await self.c.ensure_channel("billing-reminders", parent=self.c.categories["PRIVATE CONTROL"], private=True,
            topic="Private trial expiry reminders: 24 hours remaining and recently expired trials."))["id"]
        self.board = (await self.c.ensure_channel("system-status", parent=self.c.categories["PRIVATE CONTROL"], private=True))["id"]

    async def sync_health(self):
        now = time.time()
        for room, (bot, _) in list(self.c.bridge.manager.rooms.items()):
            online = bool(bot.connected and bot.chat_ready)
            health = self.portal.one("SELECT disconnect_reason FROM room_health WHERE room = ?", room)
            reason = clean(health["disconnect_reason"] if health else getattr(bot, "status", "connecting"), 60)
            code = getattr(bot, "room_http_error", None)
            current = "online" if online else "offline:" + (str(code) if code else reason)
            key = "health:" + room
            old = self.get(key)
            if old is None:
                old = {"state": current, "since": now, "delivered": "online" if online else None}
            elif old["state"] != current:
                old = {"state": current, "since": now, "delivered": old.get("delivered")}
            if old.get("delivered") != current and (online or now - old["since"] >= 30) and now - self.started >= 60:
                label = "RECOVERED" if online else "ACCESS DENIED" if code == 403 else "ROOM OFFLINE"
                title = clean(getattr(bot, "room_name", room), 100)
                detail = "Chat is ready again." if online else ("IMVU HTTP " + str(code) if code else reason or "Reconnecting")
                self.queue("health:" + room + ":" + str(old["since"]) + ":" + current,
                           self.alerts, f"**{label}**\n{title} | `{room}`\n{detail}\nObserved <t:{int(now)}:F>")
                old["delivered"] = current
            self.put(key, old)

    async def sync_reminders(self):
        now = int(time.time())
        trials = self.portal.db.execute("""SELECT r.id, a.expires, n.name FROM room_access a
            JOIN rooms r ON r.id = a.room JOIN accounts u ON u.email = r.customer
            LEFT JOIN room_names n ON n.room = r.id
            WHERE a.trial_requested = 1 AND a.unlimited = 0 AND a.expires > 0
              AND r.status = 'approved' AND u.owner = 0
              AND NOT EXISTS (SELECT 1 FROM owner_room_terms t WHERE t.room = r.id)
              AND NOT EXISTS (SELECT 1 FROM vcoin_orders o WHERE o.room = r.id AND o.transfer IS NOT NULL)
              AND a.expires BETWEEN ? AND ?""", (now - 86400, now + 86400)).fetchall()
        for trial in trials:
            stage = "expired" if trial["expires"] <= now else "24h"
            label = "TRIAL EXPIRED" if stage == "expired" else "TRIAL ENDING WITHIN 24 HOURS"
            self.queue(f"trial:{trial['id']}:{trial['expires']}:{stage}", self.reminders,
                f"**{label}**\n{clean(trial['name'] or trial['id'], 100)} | `{trial['id']}`\n"
                f"Trial end: <t:{trial['expires']}:F> (<t:{trial['expires']}:R>)\nReview access on https://ramzybots.site/")

    async def sync_board(self):
        lines = ["**RamzyBots • Room status**", "Updates every ~20 seconds. Occupants exclude the bot when identified."]
        for room, (bot, _) in list(self.c.bridge.manager.rooms.items()):
            online = bool(bot.connected and bot.chat_ready)
            members = set(getattr(bot, "members", ())) - {str(getattr(bot, "user_id", ""))}
            radio = getattr(bot, "radio", None)
            track = getattr(radio, "current", None)
            title = clean(track.get("title"), 90) if track else "Nothing playing"
            count = str(len(members)) if online else "unknown"
            code = getattr(bot, "room_http_error", None)
            state = "ONLINE" if online else "HTTP " + str(code) if code else "CONNECTING / OFFLINE"
            lines.append(f"\n**{clean(getattr(bot, 'room_name', room), 70)}** · `{room}`\n{state} · Occupants: {count}\n♪ {title}")
        if len(lines) == 2:
            lines.append("No rooms are currently managed.")
        pages, page = [], []
        for line in lines:
            if size("\n".join([*page, line])) > 1750:
                pages.append("\n".join(page))
                page = ["**Room status • continued**"]
            page.append(line)
        pages.append("\n".join(page))
        previous = self.get("board_pages", 0)
        for i in range(max(previous, len(pages))):
            text = pages[i] if i < len(pages) else "No additional rooms. See the status board above."
            state = self.get("board:" + str(i), {})
            if state.get("text") == text:
                continue
            payload = {"content": text, "allowed_mentions": {"parse": []}, "flags": 4}
            if state.get("message"):
                try:
                    await self.c.api("PATCH", f"/channels/{self.board}/messages/{state['message']}", json=payload)
                except DiscordError as error:
                    if error.code != 404:
                        raise
                    state = {}
            if not state.get("message"):
                payload.update(nonce=hashlib.sha256((self.c.guild + ":board:" + str(i)).encode()).hexdigest()[:24], enforce_nonce=True)
                state["message"] = (await self.c.api("POST", "/channels/" + self.board + "/messages", json=payload))["id"]
            self.put("board:" + str(i), {"message": state["message"], "text": text})
        self.put("board_pages", len(pages))

    def history(self, order_id):
        events = self.portal.db.execute("SELECT * FROM vcoin_review_history WHERE order_id = ? ORDER BY id DESC LIMIT 8", (order_id,)).fetchall()
        return "**Ticket action history**\n" + ("\n".join(
            f"<t:{e['created']}:f> · {e['action']} · owner `{e['actor']}`\n{clean(e['note'], 90)}" for e in reversed(events)) or "No review actions recorded yet.")

    async def review(self, order_id, event, action, note):
        if action not in {"reject", "proof", "close", "reopen"} or not note or len(note) > 300:
            raise ValueError("Include a reason of 1-300 characters.")
        async with self.portal.lock:
            order = self.portal.one("SELECT * FROM vcoin_orders WHERE id = ?", order_id)
            if not order or str(order["report_event"] or "") != event:
                raise ValueError("This ticket changed. Use the latest order card.")
            old = self.portal.one("SELECT * FROM vcoin_reviews WHERE order_id = ?", order_id)
            if old and old["event"] == event and old["state"] == action:
                raise ValueError("This action was already recorded.")
            if action == "reopen" and (not old or old["event"] != event or old["state"] != "close"):
                raise ValueError("This ticket is not closed.")
            if action in {"reject", "proof"}:
                if old and old["event"] == event and old["state"] == "close":
                    raise ValueError("Reopen this ticket first.")
                if order["transfer"] is not None or not order["reported_sender"]:
                    raise ValueError("Only an unconfirmed payment report can be rejected or returned for proof.")
                owner = self.portal.one("SELECT * FROM accounts WHERE owner = 1 LIMIT 1")
                if not owner:
                    raise ValueError("Website owner is not configured.")
                await self.portal.room_action(owner, "return-vcoin-report", order["room"], {"order": order_id})
            now = int(time.time())
            self.portal.db.execute("BEGIN IMMEDIATE")
            with self.portal.db:
                self.portal.db.execute("INSERT INTO vcoin_reviews VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET event=excluded.event,state=excluded.state,note=excluded.note,actor=excluded.actor,created=excluded.created",
                                       (order_id, event, action, note, self.c.owner, now))
                self.record(order_id, event, action, note, now)

    def record(self, order_id, event, action, note, now=None):
        self.portal.db.execute("INSERT INTO vcoin_review_history(order_id,event,action,note,actor,created) VALUES (?, ?, ?, ?, ?, ?)",
                               (order_id, event, action, note, self.c.owner, int(time.time()) if now is None else now))
