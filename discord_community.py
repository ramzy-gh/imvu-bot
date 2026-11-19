"""Private IMVU room channels, Discord interactions, and canonical website VCOIN tickets."""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import time

import aiohttp

from discord_bridge import DiscordError
from permissions import OWNER_ID, OwnerCommand

LOG = logging.getLogger("ramsei.discord.community")
VIEW, SEND, HISTORY = 1024, 2048, 65536
CRIMSON = 0xE62B4A
DENIED = "You can't reply to imvu rooms because you are not logged into RamzMotionz account"


def button(label, custom_id=None, *, style=2, url=None, disabled=False):
    value = {"type": 2, "style": 5 if url else style, "label": label, "disabled": disabled}
    value["url" if url else "custom_id"] = url or custom_id
    return value


def row(*buttons):
    return {"type": 1, "components": list(buttons)}


class DiscordCommunity:
    def __init__(self, bridge):
        self.bridge = bridge
        self.guild = os.getenv("DISCORD_GUILD_ID", "").strip()
        self.owner = bridge.owner
        self.bot_id = bridge.bot_id
        self.api = bridge.api
        self.channels = []
        self.categories = {}
        self.room_channels = {}
        self.room_labels = {}
        self.room_lock = asyncio.Lock()
        self.session_id = self.resume_url = None
        self.sequence = None
        self.acked = True
        self.interactions = set()
        self.seen = {}
        self.portal = getattr(bridge.manager, "portal", None)
        self.order_cursor = 0
        self.message_lock = asyncio.Lock()
        self.message_content = False
        self.last_content_notice = -60
        if self.portal:
            self.portal.db.execute("""CREATE TABLE IF NOT EXISTS discord_order_tickets (
                order_id TEXT PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL,
                message TEXT, fingerprint TEXT, closed INTEGER NOT NULL DEFAULT 0)""")
            self.portal.db.execute("""CREATE TABLE IF NOT EXISTS discord_trial_notifications (
                guild TEXT NOT NULL, room TEXT NOT NULL, message TEXT NOT NULL,
                PRIMARY KEY(guild, room))""")
        from discord_ops import DiscordOperations
        self.ops = DiscordOperations(self)

    def private_overwrites(self, existing=()):
        viewers = [{**o, "allow": str(int(o.get("allow", 0)) & ~SEND),
                    "deny": str(int(o.get("deny", 0)) | SEND)} for o in existing
                   if str(o["id"]) not in {self.guild, self.owner, self.bot_id}]
        return [{"id": self.guild, "type": 0, "deny": str(VIEW | SEND), "allow": "0"},
                {"id": self.owner, "type": 1, "deny": "0", "allow": str(VIEW | SEND | HISTORY)},
                {"id": self.bot_id, "type": 1, "deny": "0", "allow": str(VIEW | SEND | HISTORY)}, *viewers]

    async def ensure_channel(self, name, kind=0, parent=None, private=False, readonly=False, topic=None):
        found = next((c for c in self.channels if c.get("name") == name and c.get("type") == kind
                      and (not parent or c.get("parent_id") == parent)), None)
        payload = {"name": name, "type": kind}
        if parent:
            payload["parent_id"] = parent
        if private:
            payload["permission_overwrites"] = self.private_overwrites((found or {}).get("permission_overwrites", ()))
        elif readonly:
            payload["permission_overwrites"] = [
                {"id": self.guild, "type": 0, "deny": str(SEND), "allow": "0"},
                {"id": self.bot_id, "type": 1, "deny": "0", "allow": str(VIEW | SEND | HISTORY)}]
        if topic and kind == 0:
            payload["topic"] = topic[:1024]
        if found:
            patch = {k: v for k, v in payload.items() if k != "type" and found.get(k) != v}
            if patch:
                found.update(await self.api("PATCH", "/channels/" + found["id"], json=patch))
            return found
        result = await self.api("POST", f"/guilds/{self.guild}/channels", json=payload)
        self.channels.append(result)
        return result

    async def seed(self, channel, text):
        if not channel.get("last_message_id"):
            await self.bridge.send(text, channel=channel["id"])

    async def setup(self):
        guild = await self.api("GET", "/guilds/" + self.guild)
        if str(guild.get("owner_id")) != self.owner:
            raise DiscordError(403)
        self.channels = await self.api("GET", f"/guilds/{self.guild}/channels")
        roles = await self.api("GET", f"/guilds/{self.guild}/roles")
        for name, color, hoist in [("Founder", CRIMSON, True), ("Moderator", 0x9B59B6, True),
                                   ("Support", 0x3AAFA9, True), ("Customer", 0xF0B44D, True),
                                   ("Member", 0xBCC5D4, False)]:
            existing = next((r for r in roles if r["name"] == name), None)
            if existing is None:
                existing = await self.api("POST", f"/guilds/{self.guild}/roles", json={
                    "name": name, "color": color, "hoist": hoist, "mentionable": False,
                    "permissions": str((1 << 13) | (1 << 40)) if name == "Moderator" else "0"})
                roles.append(existing)
            if name == "Founder":
                await self.api("PUT", f"/guilds/{self.guild}/members/{self.owner}/roles/{existing['id']}")
        for old_name, new_name in [("Text Channels", "START HERE"), ("Voice Channels", "VOICE LOUNGE")]:
            old = next((c for c in self.channels if c.get("name") == old_name and c.get("type") == 4), None)
            if old and not any(c.get("name") == new_name and c.get("type") == 4 for c in self.channels):
                old.update(await self.api("PATCH", "/channels/" + old["id"], json={"name": new_name}))
        for name, private in [("START HERE", False), ("COMMUNITY", False), ("VOICE LOUNGE", False),
                              ("IMVU ROOM LOGS", True), ("PRIVATE CONTROL", True), ("VCOIN ORDERS", True)]:
            self.categories[name] = (await self.ensure_channel(name, 4, private=private))["id"]
        guides = {
            "welcome": "**Welcome to RamzyBots**\nPower your room. Own the vibe.\n"
                       "Smart IMVU bots, music, and a community built around your rooms.\n"
                       "Website: https://ramzybots.site\nStart with #rules and #bot-guide, then say hello in #general.",
            "rules": "**The house rules**\n1. Respect each other. No harassment or hate.\n"
                     "2. Keep spam, scams, and unsolicited ads out.\n3. Never post passwords, tokens, or payment evidence publicly.\n"
                     "4. Use the website for subscriptions; payment reviews stay in private tickets.\n"
                     "5. Keep voice chats welcoming and use the right channels.",
            "announcements": "**RamzyBots updates**\nProduct news, maintenance, and new features land here.",
            "bot-guide": "**Your room, upgraded**\nVisit https://ramzybots.site to add and manage your IMVU room.\n"
                         "VCOIN orders are reviewed in private tickets. A payment is confirmed only after the owner checks the IMVU Wallet.\n"
                         "Room logs are separate and private. Community members cannot read other people's room chats.\n"
                         "Need a hand? Use #help-desk. Never send a bot token or password there."}
        for name, text in guides.items():
            channel = await self.ensure_channel(name, parent=self.categories["START HERE"], readonly=True)
            await self.seed(channel, text)
        for name, topic in [("general", "Hang out with the RamzyBots community."),
                            ("showcase", "Show us your IMVU rooms, creations, and bot moments."),
                            ("suggestions", "Ideas for the next RamzyBots upgrade."),
                            ("help-desk", "General help only. Keep personal/payment details private.")]:
            # Adopt the untouched default general channel instead of creating a duplicate.
            old = next((c for c in self.channels if c.get("name") == name and c.get("type") == 0), None)
            if old and old.get("parent_id") != self.categories["COMMUNITY"]:
                old.update(await self.api("PATCH", "/channels/" + old["id"], json={"parent_id": self.categories["COMMUNITY"]}))
            await self.ensure_channel(name, parent=self.categories["COMMUNITY"], topic=topic)
        for name in ("Lounge", "Music Room", "Gaming", "Support Waiting Room"):
            if name == "Lounge" and not any(c.get("name") == name and c.get("type") == 2 for c in self.channels):
                old = next((c for c in self.channels if c.get("name") == "General" and c.get("type") == 2), None)
                if old:
                    old.update(await self.api("PATCH", "/channels/" + old["id"],
                        json={"name": name, "parent_id": self.categories["VOICE LOUNGE"]}))
            await self.ensure_channel(name, 2, self.categories["VOICE LOUNGE"])
        self.control = (await self.ensure_channel("bot-control", parent=self.categories["PRIVATE CONTROL"], private=True))["id"]
        await self.ensure_channel("system-status", parent=self.categories["PRIVATE CONTROL"], private=True)
        self.trials = (await self.ensure_channel("trial-requests", parent=self.categories["PRIVATE CONTROL"], private=True,
            topic="Private customer trial requests. Trials begin on the first successful IMVU connection."))["id"]
        await self.ops.setup()
        for room in list(self.bridge.manager.rooms):
            await self.room_channel(room)
        commands = [{"name": "rooms", "description": "List connected IMVU rooms", "type": 1},
                    {"name": "status", "description": "Check the relay and pending messages", "type": 1},
                    {"name": "help", "description": "Show owner commands", "type": 1},
                    {"name": "control", "description": "Open controls for an IMVU room", "type": 1,
                     "options": [{"name": "room", "description": "IMVU room ID", "type": 3, "required": True}]},
                    {"name": "say", "description": "Send a message to an IMVU room", "type": 1,
                     "options": [{"name": "room", "description": "IMVU room ID", "type": 3, "required": True},
                                 {"name": "message", "description": "Your message", "type": 3, "required": True}]},
                    {"name": "command", "description": "Owner room command: quiet on, pick 2, imvu !admins, etc.", "type": 1,
                     "options": [{"name": "room", "description": "IMVU room ID", "type": 3, "required": True},
                                 {"name": "action", "description": "Examples: quiet on, play song name, pick 2, imvu !admins", "type": 3, "required": True}]}]
        for command in commands:
            command["default_member_permissions"] = "0"
        current = await self.api("GET", f"/applications/{self.bot_id}/guilds/{self.guild}/commands")
        def normalize(value):
            return {k: value[k] for k in ("name", "description", "type", "options", "default_member_permissions") if k in value}
        if sorted([normalize(c) for c in current], key=lambda c: c["name"]) != sorted(commands, key=lambda c: c["name"]):
            await self.api("PUT", f"/applications/{self.bot_id}/guilds/{self.guild}/commands", json=commands)

    async def room_channel(self, room):
        async with self.room_lock:
            entry = self.bridge.manager.rooms.get(room)
            title = str(getattr(entry[0], "room_name", room) or room) if entry else room
            slug = re.sub(r"[^\w-]+", "-", title.casefold()).strip("-_") if title != room else "room"
            name = (slug or "room")[:99-len(room)].rstrip("-_") + "-" + room
            topic = self.bridge.header(room) + " | ramzydc: type here to send to IMVU. All other viewers are read-only."
            if room in self.room_channels and self.room_labels.get(room) == (name, topic):
                return self.room_channels[room]
            old = next((c for c in self.channels if c.get("type") == 0 and
                        c.get("parent_id") == self.categories["IMVU ROOM LOGS"] and
                        (c.get("id") == self.room_channels.get(room) or c.get("name") == "room-" + room
                         or c.get("name", "").endswith("-" + room))), None)
            if old and old.get("name") != name:
                old.update(await self.api("PATCH", "/channels/" + old["id"], json={"name": name}))
            channel = await self.ensure_channel(name, parent=self.categories["IMVU ROOM LOGS"],
                private=True, topic=topic)
            self.room_channels[room] = channel["id"]
            self.room_labels[room] = (name, topic)
            return self.room_channels[room]

    def log_buttons(self, room):
        return [row(button("Reply to IMVU", "reply|" + room, style=1), button("Room Controls", "control|" + room))]

    def panel(self, room):
        if room not in self.bridge.manager.rooms:
            raise ValueError("That room is not active.")
        prefs = self.bridge.manager.settings.get(room)
        return {"content": self.bridge.header(room) + "\nQuiet: " + str(prefs["quiet"])
                    + " | Welcomes: " + str(prefs["welcomes"]) + "\nUse /help for all owner commands.",
                "components": [row(button("Reply", "reply|" + room, style=1), button("Play / Search", "play|" + room, style=1),
                                   button("Language", "language|" + room)),
                               row(button("Quiet ON", "quiet|" + room + "|on"), button("Quiet OFF", "quiet|" + room + "|off"),
                                   button("Welcomes ON", "welcomes|" + room + "|on"), button("Welcomes OFF", "welcomes|" + room + "|off")),
                               row(button("Set Welcome", "welcome|" + room), button("Default Welcome", "resetwelcome|" + room),
                                   button("Skip", "skip|" + room), button("Stop + Clear", "stop|" + room, style=4))]}

    @staticmethod
    def help_text():
        return ("**RamzyBots • Owner Command Center**\n"
                "/rooms — connected IMVU rooms\n/control room — settings and music controls\n"
                "/say room message — speak in one room\n/status — relay status\n"
                "/command room action — advanced room command\n"
                "Examples for action: `quiet on`, `welcomes off`, `welcome default`, `language ar-eg`, "
                "`play song name`, `pick 2`, `queue`, `np`, `imvu !admins`.\n"
                  "**ramzydc:** type text or any IMVU bot command directly in its room channel. Examples: `!boot Username`, `!addadmin Username`, `!addmod Username`, `!play song`, `!help`. Admins apply to this room; Mods apply globally. Other viewers cannot send to IMVU.\n"
                  "Every room log also has **Reply to IMVU** and **Room Controls** buttons. "
                "Song searches have selection buttons.\n"
                "VCOIN tickets are private. Verify the receipt in IMVU Wallet before submitting its reference.")

    async def callback(self, interaction, kind, data=None):
        return await self.api("POST", f"/interactions/{interaction['id']}/{interaction['token']}/callback",
                              json={"type": kind, **({"data": data} if data is not None else {})})

    async def followup(self, interaction, content=None, **kwargs):
        payload = {"flags": 64, "allowed_mentions": {"parse": []}, **kwargs}
        if content is not None:
            payload["content"] = content
        return await self.api("POST", f"/webhooks/{self.bot_id}/{interaction['token']}", json=payload)

    async def modal(self, interaction, custom_id, title, label, *, maximum=430):
        await self.callback(interaction, 9, {"custom_id": custom_id, "title": title,
            "components": [{"type": 1, "components": [{"type": 4, "custom_id": "value", "style": 1,
                "label": label[:45], "required": True, "max_length": maximum}]}]})

    async def interact(self, interaction):
        uid = str((interaction.get("member", {}).get("user") or interaction.get("user") or {}).get("id", ""))
        if str(interaction.get("guild_id", "")) != self.guild or uid != self.owner:
            await self.callback(interaction, 4, {"content": DENIED, "flags": 64})
            return
        key = str(interaction["id"])
        if key in self.seen:
            return
        self.seen[key] = time.monotonic()
        self.seen = {k: v for k, v in self.seen.items() if time.monotonic() - v < 900}
        data, kind = interaction.get("data", {}), interaction.get("type")
        custom = data.get("custom_id", "")
        parts = custom.split("|")
        action = parts[0]
        room = parts[1] if len(parts) > 1 else ""
        if kind == 3 and action in {"reject", "proof", "close"} and len(parts) == 3:
            await self.modal(interaction, action + "|" + room + "|" + parts[2], "VCOIN review action",
                "Reason / private proof instructions", maximum=300)
            return
        if kind == 3 and action in {"reply", "play", "language", "welcome", "payment"}:
            if action == "payment":
                if len(parts) != 3:
                    return
                await self.modal(interaction, "confirm|" + room + "|" + parts[2],
                    "Verify VCOIN received", "Wallet ref (only after checking receipt)", maximum=80)
            else:
                labels = {"reply": "Message for this IMVU room", "play": "Song / artist / HTTPS link",
                          "language": "Language code: en, ar-eg, ru, auto", "welcome": "Welcome text; {name} and {room} work"}
                await self.modal(interaction, action + "|" + room, "RamzyBots Room Control", labels[action],
                                 maximum=300 if action == "welcome" else 430)
            return
        await self.callback(interaction, 5, {"flags": 64})
        try:
            if kind == 2:
                command = data.get("name")
                options = {o["name"]: o.get("value", "") for o in data.get("options", [])}
                if command == "control":
                    await self.followup(interaction, **self.panel(str(options.get("room", ""))))
                    return
                line = "!" + command
                if command == "say":
                    line += " " + str(options.get("room", "")) + " " + str(options.get("message", ""))
                elif command == "command":
                    action_parts = str(options.get("action", "")).strip().split(maxsplit=1)
                    if not action_parts:
                        raise ValueError("Include an action. Use /help.")
                    line = "!" + action_parts[0].lstrip("!") + " " + str(options.get("room", ""))
                    if len(action_parts) > 1:
                        line += " " + action_parts[1]
            elif kind == 3:
                if action == "history":
                    await self.followup(interaction, self.ops.history(room))
                    return
                if action == "reopen" and len(parts) == 3:
                    await self.ops.review(room, parts[2], "reopen", "Reopened by owner")
                    await self.followup(interaction, "Ticket reopened. The website order is unchanged.")
                    return
                if action == "control":
                    await self.followup(interaction, **self.panel(room))
                    return
                if action == "resetwelcome":
                    line = "!welcome " + room + " default"
                elif action in {"quiet", "welcomes"} and len(parts) == 3:
                    line = "!" + action + " " + room + " " + parts[2]
                elif action in {"skip", "stop"}:
                    line = "!" + action + " " + room
                elif action == "pick" and len(parts) == 4:
                    choice = self.bridge.choices.get(room)
                    if not choice or len(choice) < 3 or choice[2] != parts[3]:
                        raise ValueError("That song selection is stale. Search again.")
                    line = "!pick " + room + " " + parts[2]
                else:
                    raise ValueError("Unknown control.")
            elif kind == 5:
                values = {c["custom_id"]: c.get("value", "") for r in data.get("components", []) for c in r.get("components", [])}
                value = values.get("value", "").strip()
                if action == "confirm":
                    await self.confirm_order(room, parts[2] if len(parts) > 2 else "", value)
                    await self.followup(interaction, "Receipt confirmed. Website access and payment history updated.")
                    return
                if action in {"reject", "proof", "close"} and len(parts) == 3:
                    await self.ops.review(room, parts[2], action, value)
                    await self.followup(interaction, "Review recorded. No payment was confirmed. " +
                        ("Ticket closed; the order and history are preserved." if action == "close" else
                         "The customer can see your reason on the website and resubmit the payment report."))
                    return
                command = "say" if action == "reply" else action
                if command not in {"say", "play", "language", "welcome"}:
                    raise ValueError("Unknown control.")
                line = "!" + command + " " + room + " " + value
            else:
                return
            # The bridge's normal validation and owner authorization remain authoritative.
            from discord_bridge import RESPONSE
            token = RESPONSE.set(lambda text, **extra: self.followup(interaction, text, **extra))
            try:
                await self.bridge.handle({"author": {"id": self.owner}, "channel_id": self.bridge.channel, "content": line})
            finally:
                RESPONSE.reset(token)
        except ValueError as error:
            await self.followup(interaction, str(error)[:1500])
        except Exception as error:
            LOG.warning("Discord control failed: type=%s", type(error).__name__)
            with contextlib.suppress(DiscordError):
                await self.followup(interaction, "Could not complete that action. Nothing was automatically confirmed.")

    async def confirm_order(self, order_id, event, reference):
        if not self.portal:
            raise ValueError("Website billing is unavailable.")
        async with self.portal.lock:
            order = self.portal.one("SELECT * FROM vcoin_orders WHERE id = ?", order_id)
            if not order or order["transfer"] is not None:
                raise ValueError("That order is missing or already confirmed.")
            if not order["reported_sender"] or not event or order["report_event"] != event:
                raise ValueError("The payment report changed. Reopen the latest ticket before confirming.")
            review = self.portal.one("SELECT * FROM vcoin_reviews WHERE order_id = ?", order_id)
            if review and review["event"] == event and review["state"] == "close":
                raise ValueError("Reopen this ticket before confirming a payment.")
            owner = self.portal.one("SELECT * FROM accounts WHERE owner = 1 LIMIT 1")
            if not owner:
                raise ValueError("Website owner is not configured.")
            # Same guarded, transactional path as the website's owner payment inbox.
            await self.portal.room_action(owner, "confirm-vcoin", order["room"], {
                "order": order_id, "transfer": reference, "verified": "1"})
            self.portal.db.execute("INSERT INTO audit(actor, room, action) VALUES (?, ?, ?)",
                                  ("discord:" + self.owner, order["room"], "confirm-vcoin"))
            self.ops.record(order_id, event, "confirmed", "Wallet receipt verified; website access updated")

    async def sync_orders(self):
        if not self.portal:
            return
        orders = self.portal.db.execute("""SELECT o.*, o.rowid AS discord_scan_id FROM vcoin_orders o
            LEFT JOIN discord_order_tickets d ON d.order_id = o.id
            WHERE (o.transfer IS NULL OR (d.guild = ? AND (d.closed = 0 OR EXISTS (
                SELECT 1 FROM vcoin_reviews v WHERE v.order_id = o.id)))) AND o.rowid > ?
            ORDER BY o.rowid LIMIT 200""", (self.guild, self.order_cursor)).fetchall()
        for order in orders:
            order_id = order["id"]
            ticket = self.portal.one("SELECT * FROM discord_order_tickets WHERE order_id = ?", order_id)
            if ticket and ticket["guild"] != self.guild:
                LOG.warning("Discord billing ticket belongs to another guild; not shared")
                continue
            if ticket is None:
                channel = await self.ensure_channel("vcoin-" + order_id[:32],
                    parent=self.categories["VCOIN ORDERS"], private=True,
                    topic="Private payment review for order " + order_id + ". Owner confirmation required.")
                self.portal.db.execute("INSERT INTO discord_order_tickets(order_id, guild, channel) VALUES (?, ?, ?)",
                                       (order_id, self.guild, channel["id"]))
                ticket = self.portal.one("SELECT * FROM discord_order_tickets WHERE order_id = ?", order_id)
            confirmed = order["transfer"] is not None
            reported = bool(order["reported_sender"])
            event = str(order["report_event"] or "")
            review = self.portal.one("SELECT * FROM vcoin_reviews WHERE order_id = ? AND event = ?", order_id, event)
            state = review["state"] if review else ""
            closed = state == "close"
            status = "CONFIRMED" if confirmed else "READY FOR OWNER REVIEW" if reported else "AWAITING PAYMENT REPORT"
            if not confirmed and state in {"reject", "proof", "close"}:
                status = {"reject": "REPORT REJECTED • CUSTOMER CAN RESUBMIT", "proof": "PROOF REQUESTED • CUSTOMER CAN RESUBMIT", "close": "TICKET CLOSED • ORDER PRESERVED"}[state]
            fields = [{"name": "Room", "value": order["room"], "inline": True},
                      {"name": "Order amount", "value": str(order["amount"]) + " VCOIN", "inline": True},
                      {"name": "Plan", "value": str(order["duration"] // 86400) + " days", "inline": True},
                      {"name": "Reported IMVU sender", "value": order["reported_sender"] or "Not reported yet"}]
            if review:
                fields.append({"name": "Latest review", "value": str(review["note"])[:300]})
            content = {"embeds": [{"title": "VCOIN • " + order_id, "description": status,
                "color": 0x38B87C if confirmed else CRIMSON, "fields": fields,
                "footer": {"text": "Confirm only after matching the transfer in your IMVU Wallet."}}],
                "allowed_mentions": {"parse": []}, "components": [row(
                    button("Verify received payment", "payment|" + order_id + "|" + str(order["report_event"] or ""),
                           style=3, disabled=confirmed or not reported or closed),
                    button("Reject report", "reject|" + order_id + "|" + event, style=4, disabled=confirmed or not reported or closed),
                    button("Request proof", "proof|" + order_id + "|" + event, disabled=confirmed or not reported or closed)), row(
                    button("Reopen ticket" if closed else "Close ticket", ("reopen|" if closed else "close|") + order_id + "|" + event),
                    button("Action history", "history|" + order_id),
                    button("Open website", url=os.getenv("PUBLIC_BASE_URL", "https://ramzybots.site").rstrip("/") + "/owner/payments"))]}
            fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            if ticket["fingerprint"] == fingerprint:
                continue
            if ticket["message"]:
                await self.api("PATCH", f"/channels/{ticket['channel']}/messages/{ticket['message']}", json=content)
                message_id = ticket["message"]
            else:
                content["nonce"] = hashlib.sha256((self.guild + order_id).encode()).hexdigest()[:24]
                content["enforce_nonce"] = True
                sent = await self.api("POST", f"/channels/{ticket['channel']}/messages", json=content)
                message_id = sent["id"]
            self.portal.db.execute("UPDATE discord_order_tickets SET message = ?, fingerprint = ?, closed = ? WHERE order_id = ?",
                                   (message_id, fingerprint, int(confirmed), order_id))
        self.order_cursor = orders[-1]["discord_scan_id"] if len(orders) == 200 else 0

    async def sync_trials(self):
        if not self.portal:
            return
        requests = self.portal.db.execute("""SELECT r.id, u.display_name, a.expires, n.name,
                (SELECT MIN(created) FROM audit WHERE room = r.id AND action = 'request-room') AS requested
            FROM room_access a JOIN rooms r ON r.id = a.room JOIN accounts u ON u.email = r.customer
            LEFT JOIN room_names n ON n.room = r.id
            WHERE a.trial_requested = 1 AND u.owner = 0 AND NOT EXISTS (
                SELECT 1 FROM discord_trial_notifications d WHERE d.guild = ? AND d.room = r.id)
            ORDER BY r.id LIMIT 100""", (self.guild,)).fetchall()
        for request in requests:
            room = request["id"]
            fields = [{"name": "Room", "value": (request["name"] or room)[:200] + " | " + room},
                      {"name": "Customer", "value": (request["display_name"] or "Website customer")[:100]},
                      {"name": "Trial", "value": "3 days; starts on the first successful IMVU connection."}]
            if request["requested"]:
                fields.append({"name": "Requested (UTC)", "value": request["requested"]})
            payload = {"embeds": [{"title": "Customer trial request", "color": CRIMSON, "fields": fields}],
                       "allowed_mentions": {"parse": []}, "components": [row(button("Open website", url="https://ramzybots.site/"))],
                       "nonce": hashlib.sha256((self.guild + ":trial:" + room).encode()).hexdigest()[:24], "enforce_nonce": True}
            sent = await self.api("POST", "/channels/" + self.trials + "/messages", json=payload)
            self.portal.db.execute("INSERT INTO discord_trial_notifications(guild, room, message) VALUES (?, ?, ?)",
                                   (self.guild, room, sent["id"]))

    async def watch_orders(self):
        refresh = 0
        while True:
            for sync in (self.sync_orders, self.sync_trials, self.ops.sync_health, self.ops.sync_reminders, self.ops.sync_board, self.ops.flush) if self.portal else ():
                try:
                    await sync()
                except Exception as error:
                    LOG.warning("Discord website notification delayed: type=%s; website remains authoritative", type(error).__name__)
            if refresh % 3 == 0:
                for room in list(self.bridge.manager.rooms):
                    try:
                        await self.room_channel(room)
                    except DiscordError:
                        break
            refresh += 1
            await asyncio.sleep(20)

    async def room_message(self, message):
        """Only the pinned owner can speak, and only into the channel's mapped room."""
        author = message.get("author") or {}
        if (str(message.get("guild_id", "")) != self.guild or author.get("bot")
                or message.get("webhook_id") or message.get("type", 0) not in {0, 19}):
            return
        channel = str(message.get("channel_id", ""))
        room = next((r for r, c in self.room_channels.items() if c == channel), None)
        if room is None:
            return
        async with self.message_lock:
            key = "message:" + str(message.get("id", ""))
            if key in self.seen:
                return
            self.seen[key] = time.monotonic()
            self.seen = {k: v for k, v in self.seen.items() if time.monotonic() - v < 900}
            async def reply(text):
                await self.bridge.send(text, channel=channel,
                    message_reference={"message_id": str(message["id"]), "fail_if_not_exists": False})
            if str(author.get("id", "")) != self.owner:
                await reply(DENIED)
                return
            if not self.message_content:
                if time.monotonic() - self.last_content_notice >= 60:
                    await reply("Direct typing needs Message Content Intent enabled in the Discord Developer Portal. Room buttons still work.")
                    self.last_content_notice = time.monotonic()
                return
            text = str(message.get("content") or "").strip()
            if (not text or len(text) > 430 or text.startswith(("*", "/"))
                    or any(ord(c) < 32 and c != "\n" for c in text) or message.get("attachments")):
                await reply("Send text or a !command, 1-430 characters. Attachments and raw IMVU actions are not forwarded.")
                return
            entry = self.bridge.manager.rooms.get(room)
            if not entry or not entry[0].connected or not entry[0].chat_ready:
                await reply("This IMVU room is reconnecting. Nothing was sent.")
                return
            bot = entry[0]
            picking = text.isdecimal() and len(text) <= 2 and OWNER_ID in getattr(bot, "song_choices", {})
            if text.startswith("!") or picking:
                if any(ord(c) < 32 for c in text):
                    await reply("Send one bot command per message, without line breaks.")
                    return
                if bot.commands.full():
                    await reply("This room's command queue is full. Please try again shortly.")
                    return
                bot.commands.put_nowait(OwnerCommand(room, text))
                await reply("Owner command queued for this IMVU room; results appear in this room log.")
                return
            try:
                if not await bot.say(text):
                    await reply("IMVU did not accept the message. Nothing was queued for later.")
            except (RuntimeError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
                await reply("Could not send to IMVU. Check the room connection before trying again.")

    async def refresh_content_access(self, ws):
        while not ws.closed and not self.message_content:
            await asyncio.sleep(60)
            try:
                app = await self.api("GET", "/oauth2/applications/@me")
            except DiscordError:
                continue
            if int(app.get("flags", 0)) & ((1 << 18) | (1 << 19)):
                self.session_id = self.resume_url = self.sequence = None
                await ws.close()
                return

    async def heartbeat(self, ws, interval):
        await asyncio.sleep(random.random() * interval)
        while not ws.closed:
            if not self.acked:
                await ws.close()
                return
            self.acked = False
            await ws.send_json({"op": 1, "d": self.sequence})
            await asyncio.sleep(interval)

    async def gateway(self):
        gateway = await self.api("GET", "/gateway/bot")
        base = gateway["url"]
        while True:
            heartbeat = content_refresh = None
            try:
                app = await self.api("GET", "/oauth2/applications/@me")
                self.message_content = bool(int(app.get("flags", 0)) & ((1 << 18) | (1 << 19)))
                url = (self.resume_url or base).rstrip("/") + "/?v=10&encoding=json"
                async with self.bridge.session.ws_connect(url, max_msg_size=8 * 1024 * 1024) as ws:
                    hello = await ws.receive_json(timeout=20)
                    if hello.get("op") != 10:
                        raise DiscordError()
                    self.acked = True
                    heartbeat = asyncio.create_task(self.heartbeat(ws, hello["d"]["heartbeat_interval"] / 1000))
                    if self.session_id:
                        await ws.send_json({"op": 6, "d": {"token": self.bridge.token,
                            "session_id": self.session_id, "seq": self.sequence}})
                    else:
                        await ws.send_json({"op": 2, "d": {"token": self.bridge.token,
                            "intents": 1 | (1 << 9) | ((1 << 15) if self.message_content else 0),
                            "properties": {"os": "linux", "browser": "RamzyBots", "device": "RamzyBots"}}})
                    if not self.message_content:
                        content_refresh = asyncio.create_task(self.refresh_content_access(ws))
                    async for packet in ws:
                        if packet.type != aiohttp.WSMsgType.TEXT:
                            break
                        event = json.loads(packet.data)
                        if event.get("s") is not None:
                            self.sequence = event["s"]
                        op = event.get("op")
                        if op == 11:
                            self.acked = True
                        elif op == 1:
                            await ws.send_json({"op": 1, "d": self.sequence})
                        elif op == 7:
                            break
                        elif op == 9:
                            if not event.get("d"):
                                self.session_id = self.resume_url = self.sequence = None
                            break
                        elif op == 0 and event.get("t") == "READY":
                            self.session_id, self.resume_url = event["d"]["session_id"], event["d"]["resume_gateway_url"]
                            LOG.info("Discord community gateway ready; owner controls enabled; direct typing=%s", self.message_content)
                        elif op == 0 and event.get("t") == "INTERACTION_CREATE":
                            if len(self.interactions) < 16:
                                task = asyncio.create_task(self.interact(event["d"]))
                                self.interactions.add(task)
                                task.add_done_callback(self.finish_interaction)
                        elif op == 0 and event.get("t") == "MESSAGE_CREATE":
                            if len(self.interactions) < 16:
                                task = asyncio.create_task(self.room_message(event["d"]))
                                self.interactions.add(task)
                                task.add_done_callback(self.finish_interaction)
                    if ws.close_code in {4007, 4009}:
                        self.session_id = self.resume_url = self.sequence = None
                    if ws.close_code in {4004, 4013, 4014}:
                        raise DiscordError(401)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
                LOG.warning("Discord gateway reconnecting; no credentials logged")
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                if content_refresh:
                    content_refresh.cancel()
                    await asyncio.gather(content_refresh, return_exceptions=True)
            await asyncio.sleep(5)

    def finish_interaction(self, task):
        self.interactions.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error:
                LOG.warning("Discord interaction failed: type=%s", type(error).__name__)

    async def close(self):
        for task in self.interactions:
            task.cancel()
        await asyncio.gather(*self.interactions, return_exceptions=True)

