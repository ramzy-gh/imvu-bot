"""Persistent bot permissions. Usernames are never authorization credentials."""
import os
import re
import threading
from pathlib import Path
from state_store import StateStore

OWNER_ID = "214900673"
OWNER_NAME = "Ramzy"


class Permissions:
    def __init__(self, default_room=None):
        self.lock = threading.RLock()
        self.path = Path(os.getenv("PERMISSIONS_PATH", "/app/data/ramsei/permissions.json"))
        self.default_room = default_room or os.getenv("IMVU_ROOM_ID", "330286535-177").strip()
        self.admins = set()
        self.room_admins = set()
        self.blocked = set()
        self.room_moderation = {}
        self.owner_blocked = set()
        self.owner_only = False
        self.names = {OWNER_ID: OWNER_NAME}
        self.store = StateStore("permissions", self.path)
        try:
            data = self.store.load()
        except (OSError, ValueError):
            raise RuntimeError("Cannot load permissions storage; repair it before starting the bot.") from None
        if data is not None:
            try:
                self.admins = self.ids(data["admins"])
                self.room_admins = self.ids(data.get("room_admins", []))
                self.blocked = self.ids(data["blocked"])
                for room, roles in data.get("room_moderation", {}).items():
                    if not re.fullmatch(r"[1-9]\d*-\d+", room) or not isinstance(roles, dict):
                        raise ValueError("Invalid room moderation data")
                    self.room_moderation[room] = (self.ids(roles["admins"]), self.ids(roles["blocked"]))
                self.owner_blocked = self.ids(data.get("owner_blocked", []))
                self.owner_only = data.get("owner_only", False)
                if type(self.owner_only) is not bool:
                    raise ValueError("Invalid owner mode")
                labels = data.get("usernames", {})
                if not isinstance(labels, dict):
                    raise ValueError("Invalid username labels")
                self.names.update({uid: name for uid, name in labels.items()
                    if isinstance(uid, str) and re.fullmatch(r"[1-9]\d*", uid)
                    and isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", name)})
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                raise RuntimeError("Cannot read permissions file; repair it before starting the bot.") from None
        else:
            # Optional migration seed, only on the first start without a saved file.
            self.admins = self.ids([s.strip() for s in os.getenv("ADMIN_IDS", "").split(",") if s.strip()])
        self.owner_blocked.discard(OWNER_ID)
        # Old files had no room attribution. Import those grants into the main
        # room once, then use explicit room IDs even if the main room changes.
        if self.admins or self.blocked:
            admins, blocked = self.room_moderation.setdefault(self.default_room, (set(), set()))
            admins.update(self.admins)
            blocked.update(self.blocked)
        self.admins, self.blocked = self.room_moderation.setdefault(self.default_room, (set(), set()))
        for admins, blocked in self.room_moderation.values():
            admins.discard(OWNER_ID)
            blocked.difference_update(admins | {OWNER_ID})
        self.room_admins.discard(OWNER_ID)
        self.names[OWNER_ID] = OWNER_NAME
        if data is None:
            try:
                self.save()
            except OSError:
                raise RuntimeError("Cannot write permissions storage; use a writable persistent PERMISSIONS_PATH.") from None

    @staticmethod
    def ids(values):
        if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r"[1-9]\d*", v) for v in values):
            raise ValueError("Account IDs must be positive numeric strings")
        return set(values)

    def moderation(self, room=None):
        return self.room_moderation.get(room or self.default_room, (frozenset(), frozenset()))

    def is_admin(self, uid, room=None):
        return str(uid) == OWNER_ID or str(uid) in self.moderation(room)[0]

    def globally_allowed(self, uid):
        return str(uid) == OWNER_ID or (not self.owner_only and str(uid) not in self.owner_blocked)

    def allowed(self, uid, room=None):
        return self.globally_allowed(uid) and str(uid) not in self.moderation(room)[1]

    def _relevant_ids(self):
        relevant = self.room_admins | self.owner_blocked | {OWNER_ID}
        for admins, blocked in self.room_moderation.values():
            relevant.update(admins | blocked)
        return relevant

    def save(self):
        relevant = self._relevant_ids()
        self.store.save({"admins": [], "blocked": [],
            "room_moderation": {room: {"admins": sorted(admins), "blocked": sorted(blocked)}
                                for room, (admins, blocked) in self.room_moderation.items() if admins or blocked},
            "room_admins": sorted(self.room_admins),
            "owner_blocked": sorted(self.owner_blocked), "owner_only": self.owner_only,
            "usernames": {uid: name for uid, name in self.names.items() if uid in relevant}})

    def remove_room(self, room):
        with self.lock:
            if room == self.default_room:
                raise ValueError("The main room is protected.")
            roles = self.room_moderation.pop(room, None)
            if roles is None:
                return
            try:
                self.save()
            except OSError:
                self.room_moderation[room] = roles
                raise
            for uid in (roles[0] | roles[1]) - self._relevant_ids():
                self.names.pop(uid, None)

    def authorization_error(self, actor, verified, command, room=None):
        actor = str(actor)
        if not verified:
            return "لازم أتأكد من هوية الحساب من السيرفر الأول 🔒"
        if not self.allowed(actor, room):
            return "This account is blocked from using the bot."
        if command in OWNER_COMMANDS or command == "!language":
            return None if actor == OWNER_ID else "Only owner @Ramzy can use this command."
        if command in ROOM_COMMANDS:
            return None if actor == OWNER_ID or actor in self.room_admins else "Room controls require access granted by owner @Ramzy."
        if command in {"!addroomadmin", "!removeroomadmin", "!roomadmins"}:
            return None if actor == OWNER_ID else "Only owner @Ramzy can grant, revoke or list room-control access."
        if command in {"!addadmin", "!removeadmin"}:
            if actor != OWNER_ID:
                return "إضافة وحذف الأدمنز لرامزي صاحب البوت بس 👑"
        elif not self.is_admin(actor, room):
            return "الأمر ده للأدمنز بس يا نجم 🛡️"
        return None

    def label(self, uid):
        name = self.names.get(uid)
        return "@" + name if name else "حساب اسمه مش متاح حاليا"

    def manage(self, actor, verified, command, target="", username=None, room=None):
        with self.lock:
            return self._manage(actor, verified, command, target, username, room)

    def _manage(self, actor, verified, command, target, username, room):
        """IDs authorize internally; all user-facing output uses usernames."""
        error = self.authorization_error(actor, verified, command, room)
        if error:
            return error
        if command == "!owneronly":
            if target.lower() not in {"on", "off"}:
                return "Usage: !owneronly on|off"
            before_mode = self.owner_only
            self.owner_only = target.lower() == "on"
            try:
                self.save()
            except OSError:
                self.owner_only = before_mode
                return "Could not save owner-only mode; no change applied."
            return "Owner-only mode " + ("ON: only @Ramzy can use the bot." if self.owner_only else "OFF: normal permissions restored.")
        if command == "!ownerblocked":
            return "Owner-blocked: " + (", ".join(self.label(uid) for uid in sorted(self.owner_blocked)[:12]) or "none")
        if command == "!admins":
            names = [self.label(uid) for uid in sorted(self.moderation(room)[0])[:12]]
            return "👑 Owner: @Ramzy | 🛡️ Room admins: " + (", ".join(names) or "none")
        if command == "!blocked":
            names = [self.label(uid) for uid in sorted(self.moderation(room)[1])[:12]]
            return "🚫 Blocked in this room: " + (", ".join(names) or "none")
        if command == "!roomadmins":
            names = [self.label(uid) for uid in sorted(self.room_admins)[:12]]
            return "Room-control access: " + (", ".join(names) or "none") + " | Owner: @Ramzy"
        if not re.fullmatch(r"[1-9]\d*", target):
            return f"اكتب اسم الحساب بعد الأمر، مثال: {command} Username 🔎"
        if target == OWNER_ID:
            return "صلاحيات صاحب البوت ثابتة ومينفعش تتغير من الشات 👑"
        admins, blocked = self.room_moderation.setdefault(room or self.default_room, (set(), set()))
        if command == "!block" and target in admins:
            return "مينفعش تمنع أدمن. صاحب البوت يشيل رتبته الأول 🛡️"
        if command == "!addroomadmin" and target in blocked:
            return "That account is blocked. Use !unblock Username before granting room-control access."
        if target in self.owner_blocked and command in {"!unblock", "!addadmin", "!addroomadmin"}:
            return "This account is owner-blocked. Only @Ramzy can use !ownerunblock Username."
        before = self.owner_blocked.copy(), admins.copy(), self.room_admins.copy(), blocked.copy(), self.names.copy()
        if username is not None:
            if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", username):
                return "اسم الحساب غير صالح 🔎"
            self.names[target] = username
        label = self.label(target)
        if command == "!ownerblock":
            self.owner_blocked.add(target)
            reply = f"Owner-blocked {label}. Only @Ramzy can remove this block."
        elif command == "!ownerunblock":
            self.owner_blocked.discard(target)
            reply = f"Owner block removed from {label}. Normal permissions now apply."
        elif command == "!addadmin":
            admins.add(target)
            blocked.discard(target)
            reply = f"تمت إضافة {label} كأدمن 🛡️✅"
        elif command == "!removeadmin":
            admins.discard(target)
            reply = f"تم حذف رتبة الأدمن من {label} ✅"
        elif command == "!block":
            blocked.add(target)
            reply = f"تم منع {label} من استخدام أوامر البوت 🚫"
        elif command == "!unblock":
            blocked.discard(target)
            reply = f"تم السماح لـ {label} باستخدام البوت تاني ✅"
        elif command == "!addroomadmin":
            self.room_admins.add(target)
            reply = f"Granted room-control access to {label}: !join, !leave, !rooms and !mainroom."
        elif command == "!removeroomadmin":
            self.room_admins.discard(target)
            reply = f"Removed room-control access from {label}."
        else:
            return "Unknown management command"
        try:
            self.save()
        except OSError:
            self.owner_blocked, old_admins, self.room_admins, old_blocked, self.names = before
            admins.clear()
            admins.update(old_admins)
            blocked.clear()
            blocked.update(old_blocked)
            return "التغيير متحفظش، فمرجعتش أطبقه. راجع صلاحية فولدر البيانات 🔧"
        return reply


OWNER_COMMANDS = {"!ownerblock", "!ownerunblock", "!ownerblocked", "!owneronly"}
MANAGEMENT_COMMANDS = OWNER_COMMANDS | {"!addadmin", "!removeadmin", "!admins", "!block", "!unblock", "!blocked",
                       "!addroomadmin", "!removeroomadmin", "!roomadmins"}
ROOM_COMMANDS = {"!join", "!leave", "!rooms", "!mainroom"}


def normalize_management_message(text):
    """Allow / management commands if the client transmits them as chat."""
    parts = text.strip().split(maxsplit=1)
    if parts and parts[0].startswith("/") and "!" + parts[0][1:].lower() in MANAGEMENT_COMMANDS | ROOM_COMMANDS:
        return "!" + parts[0][1:].lower() + (" " + parts[1] if len(parts) > 1 else "")
    return text
