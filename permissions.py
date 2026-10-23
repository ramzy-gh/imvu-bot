"""Persistent bot permissions. Usernames are never authorization credentials."""
import json
import os
import re
import threading
from pathlib import Path
from state_store import StateStore

OWNER_ID = "214900673"
OWNER_NAME = "Ramzy"


class Permissions:
    def __init__(self):
        self.lock = threading.RLock()
        self.path = Path(os.getenv("PERMISSIONS_PATH", "/app/data/permissions.json"))
        self.admins = set()
        self.blocked = set()
        self.names = {OWNER_ID: OWNER_NAME}
        self.store = StateStore("permissions", self.path)
        try:
            data = self.store.load()
        except (OSError, ValueError):
            raise RuntimeError("Cannot load permissions storage; repair it before starting the bot.") from None
        if data is not None:
            try:
                self.admins = self.ids(data["admins"])
                self.blocked = self.ids(data["blocked"])
                labels = data.get("usernames", {})
                if not isinstance(labels, dict):
                    raise ValueError("Invalid username labels")
                self.names.update({uid: name for uid, name in labels.items()
                    if isinstance(uid, str) and re.fullmatch(r"[1-9]\d*", uid)
                    and isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", name)})
            except (OSError, ValueError, TypeError, KeyError):
                raise RuntimeError("Cannot read permissions file; repair it before starting the bot.") from None
        else:
            # Optional migration seed, only on the first start without a saved file.
            self.admins = self.ids([s.strip() for s in os.getenv("ADMIN_IDS", "").split(",") if s.strip()])
        self.admins.discard(OWNER_ID)
        self.blocked.difference_update(self.admins | {OWNER_ID})
        self.names[OWNER_ID] = OWNER_NAME

    @staticmethod
    def ids(values):
        if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r"[1-9]\d*", v) for v in values):
            raise ValueError("Account IDs must be positive numeric strings")
        return set(values)

    def is_admin(self, uid):
        return str(uid) == OWNER_ID or str(uid) in self.admins

    def allowed(self, uid):
        return str(uid) not in self.blocked

    def save(self):
        relevant = self.admins | self.blocked | {OWNER_ID}
        self.store.save({"admins": sorted(self.admins), "blocked": sorted(self.blocked),
            "usernames": {uid: name for uid, name in self.names.items() if uid in relevant}})

    def authorization_error(self, actor, verified, command):
        actor = str(actor)
        if not verified:
            return "لازم أتأكد من هوية الحساب من السيرفر الأول 🔒"
        if command in {"!addadmin", "!removeadmin"}:
            if actor != OWNER_ID:
                return "إضافة وحذف الأدمنز لرامزي صاحب البوت بس 👑"
        elif not self.is_admin(actor):
            return "الأمر ده للأدمنز بس يا نجم 🛡️"
        return None

    def label(self, uid):
        name = self.names.get(uid)
        return "@" + name if name else "حساب اسمه مش متاح حاليا"

    def manage(self, actor, verified, command, target="", username=None):
        with self.lock:
            return self._manage(actor, verified, command, target, username)

    def _manage(self, actor, verified, command, target="", username=None):
        """IDs authorize internally; all user-facing output uses usernames."""
        error = self.authorization_error(actor, verified, command)
        if error:
            return error
        if command == "!admins":
            names = [self.label(uid) for uid in sorted(self.admins)[:12]]
            return "👑 Owner: @Ramzy | 🛡️ Admins: " + (", ".join(names) or "none")
        if command == "!blocked":
            names = [self.label(uid) for uid in sorted(self.blocked)[:12]]
            return "🚫 Blocked: " + (", ".join(names) or "none")
        if not re.fullmatch(r"[1-9]\d*", target):
            return f"اكتب اسم الحساب بعد الأمر، مثال: {command} Username 🔎"
        if target == OWNER_ID:
            return "صلاحيات صاحب البوت ثابتة ومينفعش تتغير من الشات 👑"
        if command == "!block" and target in self.admins:
            return "مينفعش تمنع أدمن. صاحب البوت يشيل رتبته الأول 🛡️"
        before = self.admins.copy(), self.blocked.copy(), self.names.copy()
        if username is not None:
            if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", username):
                return "اسم الحساب غير صالح 🔎"
            self.names[target] = username
        label = self.label(target)
        if command == "!addadmin":
            self.admins.add(target)
            self.blocked.discard(target)
            reply = f"تمت إضافة {label} كأدمن 🛡️✅"
        elif command == "!removeadmin":
            self.admins.discard(target)
            reply = f"تم حذف رتبة الأدمن من {label} ✅"
        elif command == "!block":
            self.blocked.add(target)
            reply = f"تم منع {label} من استخدام أوامر البوت 🚫"
        elif command == "!unblock":
            self.blocked.discard(target)
            reply = f"تم السماح لـ {label} باستخدام البوت تاني ✅"
        else:
            return "Unknown management command"
        try:
            self.save()
        except OSError:
            self.admins, self.blocked, self.names = before
            return "التغيير متحفظش، فمرجعتش أطبقه. راجع صلاحية فولدر البيانات 🔧"
        return reply


MANAGEMENT_COMMANDS = {"!addadmin", "!removeadmin", "!admins", "!block", "!unblock", "!blocked"}


def normalize_management_message(text):
    """Allow / management commands if the client transmits them as chat."""
    parts = text.strip().split(maxsplit=1)
    if parts and parts[0].startswith("/") and "!" + parts[0][1:].lower() in MANAGEMENT_COMMANDS:
        return "!" + parts[0][1:].lower() + (" " + parts[1] if len(parts) > 1 else "")
    return text
