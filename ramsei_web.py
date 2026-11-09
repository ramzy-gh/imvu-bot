"""Ramzy Bots room-scoped web portal."""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
import unicodedata
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from html import escape
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path

from aiohttp import web

from room_control import room_id
from room_language import welcome_message
from ramzy_locale import LANGUAGES, RETURN_PATHS, locale, set_website_locale, translate, translate_html, website_locale

ROOM_LANGUAGES = {
    "auto": "Automatic", "en": "English", "ar-eg": "Egyptian Arabic", "ru": "Russian",
    "bn": "Bengali", "zh": "Chinese", "cs": "Czech", "nl": "Dutch", "fi": "Finnish",
    "fr": "French", "de": "German", "el": "Greek", "he": "Hebrew", "hi": "Hindi",
    "id": "Indonesian", "it": "Italian", "ja": "Japanese", "ko": "Korean", "fa": "Persian",
    "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "es": "Spanish",
    "sw": "Swahili", "sv": "Swedish", "th": "Thai", "tr": "Turkish", "uk": "Ukrainian",
    "ur": "Urdu", "vi": "Vietnamese",
}


def room_language_options(current):
    options = dict(ROOM_LANGUAGES)
    if current not in options:
        options[current] = current
    return "".join(
        f'<option value="{escape(code, quote=True)}"{" selected" if code == current else ""}>{escape(label)}</option>'
        for code, label in options.items()
    )

SESSION_AGE = 8 * 3600
CODE_AGE = 10 * 60
CODE_ATTEMPTS = 5
ROOM_TERM = 30 * 86400
TRIAL_TERM = 3 * 86400
VCOIN_PLANS = {"monthly": (700, ROOM_TERM), "yearly": (6720, 365 * 86400)}
BILLING_REMINDER_WINDOWS = {"free trial": 86400, "subscription": 3 * 86400, "room access": 3 * 86400}
LOG = logging.getLogger(__name__)
ROOM_HEALTH_REASONS = {
    "manual_stop": "The bot was disconnected by a room administrator.",
    "access_expired": "Room access expired.",
    "access_suspended": "Room access was suspended.",
    "room_unavailable": "IMVU denied access or the room is unavailable.",
    "sign_in_failed": "The bot could not sign in to IMVU.",
    "session_expired": "The IMVU session expired.",
    "connection_failed": "The bot could not connect to the room.",
    "connection_lost": "The room connection to IMVU was interrupted.",
    "connection_timeout": "The IMVU connection timed out.",
    "worker_stopped": "The room connection stopped unexpectedly.",
    "service_restart": "The bot service stopped or restarted.",
}
SHARE_DESCRIPTION = ("Bring your IMVU room to life with music, custom welcomes, and chat games. "
                     "Manage it all from Ramzy Bots and start with a free 3-day trial.")
SHARE_META = f"""
<link rel="canonical" href="https://ramzybots.site/">
<meta name="description" content="{SHARE_DESCRIPTION}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Ramzy Bots">
<meta property="og:title" content="Ramzy Bots">
<meta property="og:description" content="{SHARE_DESCRIPTION}">
<meta property="og:url" content="https://ramzybots.site/">
<meta property="og:image" content="https://ramzybots.site/share-card.png">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Ramzy Bots logo on a dark red background">
<meta name="twitter:card" content="summary_large_image">
"""
COOKIE = "__Host-ramsei-session"
CSRF_COOKIE = "__Host-ramsei-csrf"
VERIFICATION_COOKIE = "__Host-ramzy-verification"
RESET_COOKIE = "__Host-ramzy-password-reset"
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
COOKIE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Portal:
    def __init__(self, manager):
        self.manager = manager
        self.billing_test = os.getenv("RAMZY_VCOIN_TEST") == "1"
        self.vcoin_recipient = os.getenv("RAMZY_VCOIN_RECIPIENT", "").strip()
        self.invite = os.getenv("RAMSEI_WEB_INVITE", "")
        self.invite_only = os.getenv("RAMZY_SIGNUP_INVITE_ONLY") == "1"
        self.lock = asyncio.Lock()
        self.auth_slots = asyncio.Semaphore(2)
        self.attempts = []
        self.reset_mail_tasks = set()
        self.reset_mail_slots = asyncio.Semaphore(2)
        live_db = Path(os.getenv("RAMSEI_WEB_DB", "/app/data/ramsei/website.sqlite3"))
        if self.billing_test:
            test_db = os.getenv("RAMZY_VCOIN_TEST_DB", "").strip()
            if not test_db or Path(test_db).resolve() == live_db.resolve():
                raise RuntimeError("VCOIN test mode requires a separate test database path.")
            path = Path(test_db)
        else:
            path = live_db
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY, salt BLOB NOT NULL, password BLOB NOT NULL,
                owner INTEGER NOT NULL DEFAULT 0, display_name TEXT NOT NULL DEFAULT '',
                email_verified INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS single_owner ON accounts(owner) WHERE owner = 1;
            CREATE TABLE IF NOT EXISTS pending_signups (
                token TEXT PRIMARY KEY, email TEXT NOT NULL, salt BLOB NOT NULL,
                password BLOB NOT NULL, code TEXT NOT NULL, expires INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, display_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS pending_signup_email ON pending_signups(email);
            CREATE TABLE IF NOT EXISTS room_claims (
                email TEXT PRIMARY KEY REFERENCES accounts(email) ON UPDATE CASCADE,
                room TEXT NOT NULL, code TEXT NOT NULL UNIQUE, expires INTEGER NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES accounts(email),
                expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_email_changes (
                email TEXT PRIMARY KEY REFERENCES accounts(email) ON UPDATE CASCADE,
                new_email TEXT NOT NULL, session TEXT NOT NULL,
                stage TEXT NOT NULL, code TEXT NOT NULL, expires INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS password_resets (
                token TEXT PRIMARY KEY, address_key TEXT NOT NULL,
                email TEXT REFERENCES accounts(email) ON DELETE CASCADE,
                code TEXT NOT NULL, expires INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS password_reset_requests ON password_resets(address_key, expires);
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY, customer TEXT NOT NULL REFERENCES accounts(email),
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS grants (
                room TEXT REFERENCES rooms(id), email TEXT REFERENCES accounts(email),
                PRIMARY KEY(room, email)
            );
            CREATE TABLE IF NOT EXISTS room_names (
                room TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS room_health (
                room TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
                last_connected INTEGER, disconnect_reason TEXT,
                online INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS owner_room_terms (
                room TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE CASCADE,
                expires INTEGER
            );
            CREATE TABLE IF NOT EXISTS site_admins (
                email TEXT PRIMARY KEY REFERENCES accounts(email)
            );
            CREATE TABLE IF NOT EXISTS audit (
                created TEXT DEFAULT CURRENT_TIMESTAMP, actor TEXT NOT NULL,
                room TEXT NOT NULL, action TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS room_access (
                room TEXT PRIMARY KEY REFERENCES rooms(id),
                expires INTEGER NOT NULL DEFAULT 0,
                unlimited INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS vcoin_orders (
                id TEXT PRIMARY KEY, room TEXT NOT NULL REFERENCES rooms(id),
                amount INTEGER NOT NULL, transfer TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS retired_wallet_transfers (
                fingerprint TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS billing_email_outbox (
                kind TEXT NOT NULL,
                room TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                event TEXT NOT NULL,
                expires INTEGER NOT NULL,
                due INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER,
                PRIMARY KEY(kind, room, event)
            );
            CREATE INDEX IF NOT EXISTS pending_billing_email
                ON billing_email_outbox(due) WHERE delivered IS NULL;
        """)
        # Keep previous orders and their original price and term when upgrading.
        for table, columns in (
            ("room_access", {"trial_requested": "INTEGER NOT NULL DEFAULT 0",
                             "join_pending": "INTEGER NOT NULL DEFAULT 0"}),
            ("vcoin_orders", {"duration": f"INTEGER NOT NULL DEFAULT {ROOM_TERM}",
                              "reported_sender": "TEXT",
                              "report_event": "TEXT",
                              "confirmed_at": "INTEGER",
                              "renewed_until": "INTEGER",
                              "payer": "TEXT REFERENCES accounts(email) ON UPDATE CASCADE"}),
            ("rooms", {"gift_unclaimed": "INTEGER NOT NULL DEFAULT 0"}),
        ):
            existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_open_vcoin_order_per_payer
            ON vcoin_orders(room, COALESCE(payer, '')) WHERE transfer IS NULL""")
        self.db.execute("DROP INDEX IF EXISTS one_open_vcoin_order")
        # A previous process can stop without running its shutdown handlers.
        self.db.execute("UPDATE room_health SET online = 0, disconnect_reason = 'service_restart' WHERE online = 1")
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            for table in ("accounts", "pending_signups"):
                if not any(row["name"] == "display_name" for row in self.db.execute(f"PRAGMA table_info({table})")):
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
            if not any(row["name"] == "email_verified" for row in self.db.execute("PRAGMA table_info(accounts)")):
                self.db.execute("ALTER TABLE accounts ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        owner_email = os.getenv("RAMSEI_WEB_OWNER_EMAIL", "").strip().lower()
        self.db.execute("UPDATE accounts SET email_verified = 1 WHERE owner = 1 AND email = ?", (owner_email,))
        if not self.one("SELECT email FROM accounts WHERE owner = 1"):
            password = os.getenv("RAMSEI_WEB_OWNER_PASSWORD", "")
            self.validate_credentials(owner_email, password)
            self.create_account(owner_email, password, owner=True)

    def one(self, query, *args):
        return self.db.execute(query, args).fetchone()

    def import_owner_rooms(self):
        rooms = getattr(self.manager, "desired_rooms", set()) | set(self.manager.rooms)
        for room in sorted(rooms):
            self.register_owner_room(room)

    def register_owner_room(self, room):
        self.db.execute("""INSERT OR IGNORE INTO rooms(id, customer, status)
            SELECT ?, email, 'approved' FROM accounts WHERE owner = 1""", (room,))

    def record_room_health(self, room, reason=None):
        try:
            if reason is None:
                self.db.execute("""INSERT INTO room_health(room, last_connected, online)
                    SELECT id, ?, 1 FROM rooms WHERE id = ?
                    ON CONFLICT(room) DO UPDATE SET last_connected = excluded.last_connected, online = 1""",
                    (int(time.time()), room))
            elif reason in ROOM_HEALTH_REASONS:
                self.db.execute("""INSERT INTO room_health(room, disconnect_reason)
                    SELECT id, ? FROM rooms WHERE id = ?
                    ON CONFLICT(room) DO UPDATE SET disconnect_reason = excluded.disconnect_reason, online = 0""",
                    (reason, room))
        except sqlite3.Error as error:
            LOG.warning("Room health update failed (%s)", type(error).__name__)

    def site_admin(self, account):
        return bool(account["owner"] or self.one("SELECT 1 FROM site_admins WHERE email = ?", account["email"]))

    def room_can_join(self, room):
        if room == self.manager.default_room:
            return True
        row = self.one("SELECT status FROM rooms WHERE id = ?", room)
        if not row:
            return True
        if row["status"] != "approved":
            return False
        term = self.one("SELECT expires FROM owner_room_terms WHERE room = ?", room)
        if term:
            return term["expires"] is None or term["expires"] > time.time()
        access = self.room_access(room)
        return access is None or self.access_active(access) or self.trial_waiting(access)

    def prepare_owner_rooms(self):
        self.import_owner_rooms()
        self.manager.desired_rooms = {room for room in self.manager.desired_rooms if self.room_can_join(room)}

    async def set_owner_term(self, room, end_date):
        was_expired = not self.room_can_join(room)
        expires = None
        if end_date is not None:
            if room == self.manager.default_room:
                raise ValueError("The main room is protected and can only have no expiration.")
            try:
                last_day = date.fromisoformat(end_date)
                expires = int(datetime.combine(last_day + timedelta(days=1), day_time.min, timezone.utc).timestamp())
            except (ValueError, OverflowError):
                raise ValueError("Choose a valid expiration date.") from None
            if expires <= time.time():
                raise ValueError("Choose today or a future date in UTC.")
        self.db.execute("""INSERT INTO owner_room_terms(room, expires) VALUES (?, ?)
            ON CONFLICT(room) DO UPDATE SET expires = excluded.expires""", (room, expires))
        if was_expired:
            try:
                await self.manager.join(room)
            except (OSError, ValueError):
                logging.getLogger(__name__).warning("Renewed room %s still needs to be connected", room)

    def room_access(self, room):
        return self.one("SELECT * FROM room_access WHERE room = ?", room)

    @staticmethod
    def access_active(access, now=None):
        return bool(access and (access["unlimited"] or access["expires"] > (time.time() if now is None else now)))

    @staticmethod
    def trial_waiting(access):
        return bool(access and access["trial_requested"] and not access["expires"])

    def pause_room(self, room):
        self.db.execute("UPDATE room_access SET join_pending = 0 WHERE room = ?", (room,))
        self.record_room_health(room, "manual_stop")

    async def remove_room(self, room, actor):
        if room == self.manager.default_room:
            raise web.HTTPForbidden()
        # Keep the room blocked if the process stops before both stores are cleaned.
        self.db.execute("UPDATE rooms SET status = 'suspended' WHERE id = ?", (room,))
        try:
            await self.manager.remove_room(room)
            self.db.execute("BEGIN IMMEDIATE")
            with self.db:
                transfers = self.db.execute(
                    "SELECT transfer FROM vcoin_orders WHERE room = ? AND transfer IS NOT NULL", (room,)
                ).fetchall()
                self.db.executemany("INSERT OR IGNORE INTO retired_wallet_transfers VALUES (?)",
                                    ((digest(row["transfer"]),) for row in transfers))
                for table in ("vcoin_orders", "room_access", "grants", "room_names",
                              "owner_room_terms", "room_claims"):
                    self.db.execute(f"DELETE FROM {table} WHERE room = ?", (room,))
                self.db.execute("DELETE FROM audit WHERE room = ? AND action <> 'remove-room'", (room,))
                self.db.execute("DELETE FROM rooms WHERE id = ?", (room,))
                self.db.execute("INSERT INTO audit(actor, room, action) VALUES (?, ?, 'remove-room')",
                                (actor, room))
        except (OSError, ValueError, sqlite3.Error):
            raise ValueError("Room removal could not finish. The room is suspended. Try removing it again.") from None

    def billing_room(self, room, account):
        email = account["email"]
        if room == self.manager.default_room:
            raise ValueError("The service’s main room is reserved.")
        row = self.one("SELECT * FROM rooms WHERE id = ?", room)
        if row and (row["customer"] != email or row["status"] == "suspended"):
            raise ValueError("This room is unavailable for self-service.")
        self.db.execute("INSERT OR IGNORE INTO rooms(id, customer) VALUES (?, ?)", (room, email))

    def start_trial(self, room, account):
        email = account["email"]
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            access = self.room_access(room)
            if access and access["trial_requested"]:
                if self.one("SELECT 1 FROM rooms WHERE id = ? AND customer = ? AND status = 'approved'", room, email):
                    return
                raise ValueError("This room is unavailable for self-service.")
            if self.one("""SELECT 1 FROM room_access a JOIN rooms r ON r.id = a.room
                           WHERE r.customer = ? AND a.trial_requested = 1""", email):
                raise ValueError("This account has already used a free trial.")
            if (access and (access["expires"] or access["unlimited"])) or self.one(
                    "SELECT 1 FROM rooms WHERE id = ? AND status = 'approved'", room):
                raise ValueError("This room already has access or a subscription.")
            self.billing_room(room, account)
            self.db.execute("UPDATE rooms SET status = 'approved' WHERE id = ?", (room,))
            self.db.execute("""INSERT INTO room_access(room, trial_requested, join_pending) VALUES (?, 1, 1)
                ON CONFLICT(room) DO UPDATE SET trial_requested = 1, join_pending = 1""", (room,))

    def confirm_transfer(self, order, transfer, now):
        if not re.fullmatch(r"[a-z0-9._:-]{6,80}", transfer):
            raise ValueError("Enter a wallet transaction reference of 6 to 80 characters.")
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            room = order["room"]
            if not self.one("SELECT 1 FROM rooms WHERE id = ? AND status <> 'suspended'", room):
                raise ValueError("Restore this room before confirming a payment.")
            if (self.one("SELECT 1 FROM vcoin_orders WHERE transfer = ?", transfer)
                    or self.one("SELECT 1 FROM retired_wallet_transfers WHERE fingerprint = ?", digest(transfer))):
                raise ValueError("That transfer was already used.")
            can_join = self.room_can_join(room)
            duration = order["duration"] + (TRIAL_TERM if self.trial_waiting(self.room_access(room)) else 0)
            term = self.one("SELECT expires FROM owner_room_terms WHERE room = ?", room)
            term_end = term["expires"] if term and term["expires"] is not None else 0
            updated = self.db.execute("""UPDATE vcoin_orders SET transfer = ?, confirmed_at = ?
                WHERE id = ? AND transfer IS NULL""", (transfer, now, order["id"]))
            if updated.rowcount != 1:
                raise ValueError("This order was already confirmed.")
            self.cancel_report_alert(room, order["id"])
            self.db.execute("INSERT OR IGNORE INTO room_access(room) VALUES (?)", (room,))
            self.db.execute("""UPDATE room_access SET expires = MAX(expires, ?, ?) + ?,
                               join_pending = MAX(join_pending, ?) WHERE room = ?""",
                            (now, term_end, duration, not can_join, room))
            self.db.execute("DELETE FROM owner_room_terms WHERE room = ? AND expires IS NOT NULL", (room,))
            self.db.execute("UPDATE rooms SET status = 'approved' WHERE id = ?", (room,))
            access = self.room_access(room)
            unlimited = access["unlimited"] or self.one(
                "SELECT 1 FROM owner_room_terms WHERE room = ? AND expires IS NULL", room)
            renewed_until = 0 if unlimited else access["expires"]
            self.db.execute("UPDATE vcoin_orders SET renewed_until = ? WHERE id = ?", (renewed_until, order["id"]))
            if not self.billing_test:
                self.db.execute("""INSERT INTO billing_email_outbox(kind, room, event, expires, due)
                    VALUES ('receipt', ?, ?, ?, ?)""", (room, order["id"], renewed_until, now))

    def renewal_targets(self, room=None):
        return self.db.execute("""SELECT r.id AS room, r.customer, COALESCE(n.name || ' (' || r.id || ')', r.id) AS room_name,
            CASE WHEN t.room IS NOT NULL THEN t.expires WHEN a.unlimited = 0 THEN a.expires END AS expires,
            CASE WHEN t.room IS NOT NULL THEN 'room access'
                 WHEN a.trial_requested = 1 AND NOT EXISTS (
                     SELECT 1 FROM vcoin_orders o WHERE o.room = r.id AND o.transfer IS NOT NULL
                 ) THEN 'free trial' ELSE 'subscription' END AS access_kind
            FROM rooms r JOIN accounts u ON u.email = r.customer
            LEFT JOIN room_names n ON n.room = r.id LEFT JOIN owner_room_terms t ON t.room = r.id
            LEFT JOIN room_access a ON a.room = r.id
            WHERE r.status = 'approved' AND r.id <> ? AND u.owner = 0 AND u.email_verified = 1
              AND (? IS NULL OR r.id = ?)""", (self.manager.default_room, room, room)).fetchall()

    def queue_renewal_reminders(self, now):
        for target in self.renewal_targets():
            expires = target["expires"]
            if expires is not None and 0 < expires - now <= BILLING_REMINDER_WINDOWS[target["access_kind"]]:
                self.db.execute("""INSERT OR IGNORE INTO billing_email_outbox(kind, room, event, expires, due)
                    VALUES ('reminder', ?, ?, ?, ?)""", (target["room"], str(expires), expires, now))

    def cancel_report_alert(self, room, order):
        self.db.execute("""DELETE FROM billing_email_outbox
            WHERE kind = 'report' AND room = ? AND delivered IS NULL
            AND event = (SELECT report_event FROM vcoin_orders WHERE id = ?)""", (room, order))

    def report_alert_message(self, room, event):
        report = self.one("""SELECT o.id, o.amount, u.email
            FROM vcoin_orders o JOIN rooms r ON r.id = o.room
            JOIN accounts u ON u.owner = 1 AND u.email_verified = 1
            WHERE o.room = ? AND o.report_event = ? AND o.reported_sender IS NOT NULL
              AND o.transfer IS NULL AND r.status <> 'suspended'""", room, event)
        if not report:
            return None
        return (report["email"], "New Ramzy Bots VCOIN payment report",
                f"A customer reported a VCOIN payment for room {room}.\n\n"
                f"Order: {report['id']}\nAmount: {report['amount']:,} VCOIN\n\n"
                "Review the report in the owner payment inbox: https://ramzybots.site/owner/payments\n")

    def billing_email_message(self, job, now):
        room = job["room"]
        if job["kind"] == "report":
            return self.report_alert_message(room, job["event"])
        if job["kind"] == "receipt":
            order = self.one("""SELECT o.amount, COALESCE(o.payer, r.customer) AS recipient,
                COALESCE(n.name || ' (' || r.id || ')', r.id) AS room_name
                FROM vcoin_orders o JOIN rooms r ON r.id = o.room LEFT JOIN room_names n ON n.room = r.id
                WHERE o.id = ? AND o.room = ? AND o.transfer IS NOT NULL""", job["event"], room)
            if not order:
                return None
            recipient = order["recipient"]
            end = datetime.fromtimestamp(job["expires"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if job["expires"] else "No expiration"
            subject = "Ramzy Bots VCOIN payment receipt"
            body = (f"Your VCOIN payment was confirmed.\n\nRoom: {order['room_name']}\n"
                    f"Order: {job['event']}\nAmount paid: {order['amount']:,} VCOIN\n"
                    f"Room access expires: {end}\n")
        else:
            targets = self.renewal_targets(room)
            if not targets or targets[0]["expires"] != job["expires"] or job["expires"] <= now:
                return None
            target = targets[0]
            recipient = target["customer"]
            end = datetime.fromtimestamp(job["expires"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            kind = target["access_kind"]
            subject = f"Your Ramzy Bots {kind} ends soon"
            body = (f"Your {kind} for {target['room_name']} ends at {end}.\n"
                    "You can renew room access from your Ramzy Bots control panel.\n")
        return recipient, subject, body + "\nControl panel: https://ramzybots.site/panel\n"

    async def send_billing_emails(self, now=None):
        if self.billing_test or not all(os.getenv(key) for key in (
                "RAMZY_SMTP_HOST", "RAMZY_SMTP_EMAIL", "RAMZY_SMTP_PASSWORD")):
            return
        fixed_time = now
        now = int(time.time()) if now is None else now
        self.queue_renewal_reminders(now)
        jobs = self.db.execute("""SELECT b.* FROM billing_email_outbox b
            JOIN rooms r ON r.id = b.room
            LEFT JOIN vcoin_orders o ON b.kind = 'receipt' AND o.id = b.event
            WHERE b.delivered IS NULL AND b.due <= ? AND EXISTS (
                SELECT 1 FROM accounts u WHERE u.email_verified = 1 AND (
                    (b.kind = 'report' AND u.owner = 1)
                    OR (b.kind <> 'report' AND u.email = COALESCE(o.payer, r.customer))
                )
            )
            ORDER BY b.due, b.kind LIMIT 5""", (now,)).fetchall()
        for job in jobs:
            now = int(time.time()) if fixed_time is None else fixed_time
            key = (job["kind"], job["room"], job["event"])
            message = self.billing_email_message(job, now)
            if message is None:
                self.db.execute("""DELETE FROM billing_email_outbox
                    WHERE kind = ? AND room = ? AND event = ? AND delivered IS NULL""", key)
                continue
            # A persisted lease keeps a second worker or a quick restart from resending in-flight mail.
            lease = now + 120
            claimed = self.db.execute("""UPDATE billing_email_outbox SET due = ?, attempts = attempts + 1
                WHERE kind = ? AND room = ? AND event = ? AND delivered IS NULL AND due <= ?""",
                                      (lease, *key, now))
            if not claimed.rowcount:
                continue
            try:
                await asyncio.to_thread(self.send_mail, *message, message_id=digest("|".join(key)))
            except (OSError, KeyError, ValueError, HeaderParseError) as error:
                LOG.warning("Billing email delivery failed (%s)", type(error).__name__)
                delay = min(60 * 2 ** min(job["attempts"], 6), 3600)
                self.db.execute("""UPDATE billing_email_outbox SET due = ?
                    WHERE kind = ? AND room = ? AND event = ? AND due = ? AND delivered IS NULL""",
                                (now + delay, *key, lease))
            else:
                self.db.execute("""UPDATE billing_email_outbox SET delivered = ?
                    WHERE kind = ? AND room = ? AND event = ? AND due = ? AND delivered IS NULL""",
                                (now, *key, lease))

    async def watch_billing_email(self):
        while True:
            try:
                await self.send_billing_emails()
            except sqlite3.Error as error:
                LOG.warning("Billing email check failed (%s)", type(error).__name__)
            await asyncio.sleep(60)

    async def reconcile_access(self):
        async with self.lock:
            desired = self.manager.desired_rooms | set(self.manager.rooms)
            pending = {row["room"] for row in self.db.execute("SELECT room FROM room_access WHERE join_pending = 1")}
            active, expired = [], []
            for room in sorted(desired | pending):
                (active if self.room_can_join(room) else expired).append(room)
            for room in expired:
                try:
                    if room in desired:
                        await self.manager.leave(room)
                        status = self.one("SELECT status FROM rooms WHERE id = ?", room)
                        reason = "access_suspended" if status and status["status"] == "suspended" else "access_expired"
                        self.record_room_health(room, reason)
                except (ValueError, OSError):
                    LOG.warning("Could not apply subscription access for room %s; will retry", room)
            for room in active:
                running = self.manager.rooms.get(room)
                access = self.room_access(room)
                join_pending = access and access["join_pending"]
                waiting = self.trial_waiting(access)
                try:
                    if (join_pending or room in self.manager.desired_rooms) and (not running or running[1].done()):
                        await self.manager.join(room)
                    if join_pending or waiting:
                        current = self.manager.rooms.get(room)
                        if waiting and not (
                                current and getattr(current[0], "connected", False) and
                                getattr(current[0], "chat_ready", False)):
                            continue
                        self.db.execute("""UPDATE room_access SET join_pending = 0,
                            expires = CASE WHEN trial_requested AND expires = 0 THEN ? ELSE expires END
                            WHERE room = ?""", (int(time.time()) + TRIAL_TERM, room))
                except (ValueError, OSError):
                    LOG.warning("Could not apply subscription access for room %s; will retry", room)

    async def watch_access(self):
        while True:
            await self.reconcile_access()
            await asyncio.sleep(5)

    def filter_startup_rooms(self):
        self.manager.desired_rooms = {room for room in self.manager.desired_rooms if self.room_can_join(room)}

    @staticmethod
    def validate_credentials(email, password):
        if len(email) > 254 or not EMAIL.fullmatch(email) or not 12 <= len(password) <= 256:
            raise ValueError("Enter a valid email and a password of 12 to 256 characters.")

    def create_account(self, email, password, owner=False):
        salt = secrets.token_bytes(16)
        self.db.execute("INSERT INTO accounts(email, salt, password, owner, email_verified) VALUES (?, ?, ?, ?, ?)",
                        (email, salt, password_hash(password, salt), owner, owner))

    @staticmethod
    def validate_display_name(value):
        name = unicodedata.normalize("NFC", value.strip())
        if not 1 <= len(name) <= 40 or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in name):
            raise ValueError("Choose a display name of 1 to 40 characters.")
        return name

    @staticmethod
    def csrf_token(request):
        token = request.cookies.get(CSRF_COOKIE, "")
        if COOKIE_TOKEN.fullmatch(token):
            return token
        if "ramzy_csrf" not in request:
            request["ramzy_csrf"] = secrets.token_urlsafe(32)
        return request["ramzy_csrf"]

    def account(self, request):
        token = request.cookies.get(COOKIE, "")
        if not COOKIE_TOKEN.fullmatch(token):
            return None
        return self.one("""SELECT a.* FROM sessions s JOIN accounts a USING(email)
                           WHERE token = ? AND expires > ?""", digest(token), time.time())

    def page(self, title, content, status=200, *, request=None):
        language, path = locale.get()
        direction = "rtl" if language == "ar" else "ltr"
        target = path if path in RETURN_PATHS else "/"
        if path not in RETURN_PATHS and request is not None and request.method == "POST" and self.account(request):
            target = "/owner" if path.startswith("/owner/") else "/panel"
        options = "".join(
            f'<option value="{code}" lang="{code}"{" selected" if code == language else ""}>{name}</option>'
            for code, name in LANGUAGES.items()
        )
        account = self.account(request) if request is not None else None
        if account:
            name = account["display_name"]
            greeting = f'Hello, <bdi>{escape(name)}</bdi>' if name else "Hello"
            navigation = ('<a href="/panel">Control panel</a>'
                          f'<a href="/account" class="account-nav">{greeting}</a>')
            if self.site_admin(account):
                navigation += '<a href="/owner">Admin panel</a>'
        else:
            navigation = '<a href="/signin">Sign in</a>'
        share_meta = SHARE_META if path == "/" else ""
        html = f"""<!doctype html>
<html lang="{language}" dir="{direction}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(translate(title, language))} | Ramzy Bots</title><link rel="stylesheet" href="/site.css?v=account-1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
{share_meta}<script src="/site-language.js?v=red-1" defer></script></head>
<body><div class="pointer-fx" aria-hidden="true"><span></span></div>
<header><a class="brand" href="/"><span class="brand-mark" aria-hidden="true">R</span><span class="brand-word">Ramzy Bots</span></a>
<nav class="main-nav"><a href="/guide">Command guide</a>{navigation}</nav>
<form class="site-languages" action="/locale" method="get"><input type="hidden" name="next" value="{target}">
<label>Website language<select name="language">{options}</select></label></form></header>
<main>{content}</main><footer>Ramzy Bots · IMVU room companion</footer></body></html>"""
        response = web.Response(status=status, content_type="text/html", text=translate_html(html, language),
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                     "Referrer-Policy": "same-origin",
                     "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        if request is not None and "ramzy_csrf" in request:
            response.set_cookie(CSRF_COOKIE, request["ramzy_csrf"], secure=True, httponly=True, samesite="Lax", path="/")
        return response

    def form(self, request, action, fields, button, *, csrf=None):
        token = self.csrf_token(request) if csrf is None else csrf
        return (f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{escape(token, quote=True)}">'
                f'{fields}<button>{button}</button></form>')

    def auth_page(self, request, signup=False, error="", status=None, *, email="", name=""):
        token = self.csrf_token(request)
        fields = (f'<label>Email<input type="email" name="email" value="{escape(email, quote=True)}" '
                  'maxlength="254" required autocomplete="email"></label>')
        autocomplete = "new-password" if signup else "current-password"
        fields += f'<label>Password<input type="password" name="password" maxlength="256" required autocomplete="{autocomplete}"></label>'
        if signup:
            fields = (f'<label>Display name<input name="name" value="{escape(name, quote=True)}" '
                      'maxlength="40" required autocomplete="nickname"></label>' + fields)
        if signup and self.invite_only:
            fields += '<label>Beta invite code<input name="invite" maxlength="256" required></label>'
        title = "Create account" if signup else "Sign in"
        route = "/signup" if signup else "/signin"
        fields = f'<input type="hidden" name="csrf" value="{token}">' + fields
        other = ('<a href="/signin">Already have an account?</a>' if signup else
                 '<p><a href="/forgot-password">Forgot password?</a></p><a href="/signup">Create account</a>')
        response = self.page(title, f'<section class="card narrow"><h1>{title}</h1><p>{escape(error)}</p>'
                            f'<form method="post" action="{route}">{fields}<button>{title}</button></form>{other}</section>',
                            status if status is not None else (400 if error else 200), request=request)
        response.set_cookie(CSRF_COOKIE, token, secure=True, httponly=True, samesite="Lax", path="/")
        return response

    def allow_auth_attempt(self, operation, email):
        key = (operation, email)
        now = time.monotonic()
        self.attempts = [(when, target) for when, target in self.attempts if when > now - 60]
        if (sum(target == key for _, target in self.attempts) >= 10 or
                sum(target[0] == operation for _, target in self.attempts) >=
                (30 if operation in {"signup", "forgot-password"} else 120)):
            return False
        self.attempts.append((now, key))
        return True

    async def auth(self, request):
        signup = request.path == "/signup"
        if self.account(request):
            raise web.HTTPSeeOther("/account" if signup else "/panel")
        if request.method == "GET":
            if not signup and request.query.get("reset") == "1":
                return self.auth_page(request, error="Password updated. Sign in with your new password.", status=200)
            return self.auth_page(request, signup)
        data = await self.checked_form(request)
        email = data.get("email", "").strip().lower()
        name = data.get("name", "")
        if not self.allow_auth_attempt("signup" if signup else "signin", email):
            return self.auth_page(request, signup, "Try again in a minute.", status=429, email=email, name=name)
        password = data.get("password", "")
        try:
            self.validate_credentials(email, password)
            if signup:
                if self.invite_only and (not self.invite or not hmac.compare_digest(
                        data.get("invite", "").encode(), self.invite.encode())):
                    raise ValueError("An active beta invite code is required.")
                name = self.validate_display_name(name)
                return await self.begin_verification(request, email, password, name)
            account = self.one("SELECT * FROM accounts WHERE email = ?", email)
            salt = account["salt"] if account else bytes(16)
            async with self.auth_slots:
                hashed = await asyncio.to_thread(password_hash, password, salt)
            if account is None or not hmac.compare_digest(account["password"], hashed):
                raise ValueError("Email or password is incorrect.")
        except ValueError as error:
            return self.auth_page(request, signup, str(error), email=email, name=name)
        token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM sessions WHERE expires <= ?", (now := int(time.time()),))
        session = self.db.execute("""INSERT INTO sessions(token, email, expires)
            SELECT ?, email, ? FROM accounts WHERE email = ? AND password = ?""",
            (digest(token), now + SESSION_AGE, email, hashed))
        if not session.rowcount:
            return self.auth_page(request, error="Email or password is incorrect.", email=email)
        response = web.HTTPSeeOther("/panel")
        response.set_cookie(COOKIE, token, max_age=SESSION_AGE, secure=True, httponly=True, samesite="Lax", path="/")
        raise response

    @staticmethod
    def send_signup_code(email, code, language):
        Portal.send_account_code(email, code, language, "Your Ramzy Bots verification code")

    @staticmethod
    def send_account_code(email, code, language, subject):
        subject = translate(subject, language)
        Portal.send_mail(email, subject, f"{subject}: {code}\n\n"
                         f"{translate('This code expires in 10 minutes.', language)}\n")

    @staticmethod
    def send_mail(email, subject, body, *, message_id=None):
        host = os.environ["RAMZY_SMTP_HOST"]
        sender = os.environ["RAMZY_SMTP_EMAIL"]
        message = EmailMessage()
        sender_address = Address("Ramzy Bots", addr_spec=sender)
        message["From"] = sender_address
        message["To"] = Address(addr_spec=email)
        message["Subject"] = subject
        if message_id:
            message["Message-ID"] = f"<{message_id}@{sender_address.domain}>"
        message.set_content(body)
        with smtplib.SMTP_SSL(host, int(os.getenv("RAMZY_SMTP_PORT", "465")),
                              timeout=10, context=ssl.create_default_context()) as smtp:
            smtp.login(sender, os.environ["RAMZY_SMTP_PASSWORD"])
            smtp.send_message(message)

    @staticmethod
    def verification_code_digest(token, code):
        return hmac.new(token.encode(), code.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def validate_email_address(email):
        # Reject recipient syntax that could otherwise be interpreted as an email header.
        if len(email) > 254 or not EMAIL.fullmatch(email) or not email.isascii():
            raise ValueError("Enter a valid email address.")
        try:
            address = Address(addr_spec=email)
            if address.addr_spec != email:
                raise ValueError
        except (HeaderParseError, ValueError):
            raise ValueError("Enter a valid email address.") from None

    async def begin_verification(self, request, email, password, name):
        self.validate_email_address(email)
        unavailable = "We couldn't send a code right now. Try again later."
        if not all(os.getenv(key) for key in ("RAMZY_SMTP_HOST", "RAMZY_SMTP_EMAIL", "RAMZY_SMTP_PASSWORD")):
            return self.auth_page(request, True, unavailable, status=503, email=email, name=name)
        token = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_bytes(16)
        async with self.auth_slots:
            hashed = await asyncio.to_thread(password_hash, password, salt)
        now = int(time.time())
        self.db.execute("DELETE FROM pending_signups WHERE expires <= ?", (now,))
        key = digest(token)
        inserted = self.db.execute("""
            INSERT INTO pending_signups(token, email, salt, password, code, expires, display_name)
            SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (
                SELECT 1 FROM pending_signups WHERE email = ? AND expires > ?
            )""", (key, email, salt, hashed, self.verification_code_digest(token, code),
                   now + CODE_AGE, name, email, now + CODE_AGE - 60))
        if not inserted.rowcount:
            return self.auth_page(request, True, "Wait a minute before requesting another code.", status=429,
                                  email=email, name=name)
        try:
            await asyncio.to_thread(self.send_signup_code, email, code, locale.get()[0])
        except (OSError, ValueError, KeyError) as error:
            LOG.warning("Registration email delivery failed (%s)", type(error).__name__)
            self.db.execute("DELETE FROM pending_signups WHERE token = ?", (key,))
            return self.auth_page(request, True, unavailable, status=503, email=email, name=name)
        response = web.HTTPSeeOther("/verify-email")
        response.set_cookie(VERIFICATION_COOKIE, token, max_age=CODE_AGE, secure=True,
                            httponly=True, samesite="Strict", path="/")
        raise response

    def verification_page(self, request, error="", can_verify=True, status=200):
        title = "Verify your email"
        token = self.csrf_token(request)
        form = ""
        if can_verify:
            form = (f'<form method="post" action="/verify-email"><input type="hidden" name="csrf" value="{token}">'
                    '<label>Verification code<input name="code" inputmode="numeric" autocomplete="one-time-code" '
                    'pattern="[0-9]{6}" maxlength="6" required></label><button>Verify email</button></form>')
        message = error or "Enter the six-digit code we sent to your email."
        response = self.page(title, f'<section class="card narrow"><h1>{title}</h1><p>{escape(message)}</p>'
                             f'{form}<a href="/signup">Request a new code</a></section>', status, request=request)
        response.set_cookie(CSRF_COOKIE, token, secure=True, httponly=True, samesite="Lax", path="/")
        return response

    async def verify_email(self, request):
        data = await self.checked_form(request) if request.method == "POST" else None
        token = request.cookies.get(VERIFICATION_COOKIE, "")
        invalid = "This code has expired or is no longer available. Request a new code."
        if not COOKIE_TOKEN.fullmatch(token):
            return self.verification_page(request, invalid, can_verify=False, status=400)
        key = digest(token)
        now = int(time.time())
        if data is None:
            pending = self.one("SELECT 1 FROM pending_signups WHERE token = ? AND expires > ? AND attempts < ?",
                               key, now, CODE_ATTEMPTS)
            return (self.verification_page(request) if pending else
                    self.verification_page(request, invalid, can_verify=False, status=400))
        code = data.get("code", "").strip()
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            pending = self.one("SELECT * FROM pending_signups WHERE token = ? AND expires > ? AND attempts < ?",
                               key, now, CODE_ATTEMPTS)
            if not pending:
                return self.verification_page(request, invalid, can_verify=False, status=400)
            if not re.fullmatch(r"[0-9]{6}", code) or not hmac.compare_digest(
                    pending["code"], self.verification_code_digest(token, code)):
                self.db.execute("UPDATE pending_signups SET attempts = attempts + 1 WHERE token = ?", (key,))
                if pending["attempts"] + 1 == CODE_ATTEMPTS:
                    return self.verification_page(request, invalid, can_verify=False, status=400)
                return self.verification_page(request, "That code is incorrect. Try again.", status=400)
            self.db.execute("""INSERT INTO accounts(email, salt, password, display_name, email_verified)
                VALUES (?, ?, ?, ?, 1) ON CONFLICT(email) DO NOTHING""",
                (pending["email"], pending["salt"], pending["password"], pending["display_name"]))
            self.db.execute("DELETE FROM pending_signups WHERE email = ?", (pending["email"],))
        response = web.HTTPSeeOther("/signin")
        response.del_cookie(VERIFICATION_COOKIE, secure=True, httponly=True, samesite="Strict", path="/")
        raise response

    def queue_password_reset_mail(self, sender, *args):
        if len(self.reset_mail_tasks) >= 16:
            return
        task = asyncio.create_task(self.deliver_password_reset_mail(sender, *args))
        self.reset_mail_tasks.add(task)
        task.add_done_callback(self.reset_mail_tasks.discard)

    async def deliver_password_reset_mail(self, sender, *args):
        async with self.reset_mail_slots:
            try:
                await asyncio.to_thread(sender, *args)
            except (OSError, ValueError, KeyError) as error:
                LOG.warning("Password recovery email delivery failed (%s)", type(error).__name__)

    async def close_password_reset_mail(self):
        tasks = tuple(self.reset_mail_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def create_password_reset(self, email):
        now = int(time.time())
        address_key = digest(email)
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            self.db.execute("DELETE FROM password_resets WHERE expires <= ?", (now,))
            previous = self.one("""SELECT COUNT(*) AS count, MAX(expires) AS latest
                FROM password_resets WHERE address_key = ?""", address_key)
            if previous["count"] >= 3 or (previous["latest"] or 0) > now + CODE_AGE - 60:
                return None
            account = self.one("SELECT email FROM accounts WHERE email = ? AND email_verified = 1", email)
            recipient = account["email"] if account else None
            token = secrets.token_urlsafe(32)
            code = f"{secrets.randbelow(1_000_000):06d}"
            # Use the same challenge shape for unknown addresses without storing their email.
            self.db.execute("""INSERT INTO password_resets(token, address_key, email, code, expires)
                VALUES (?, ?, ?, ?, ?)""",
                (digest(token), address_key, recipient, self.verification_code_digest(token, code), now + CODE_AGE))
        return token, code, recipient

    async def forgot_password(self, request):
        if request.method == "GET":
            form = self.form(request, "/forgot-password",
                '<label>Email<input type="email" name="email" maxlength="254" required autocomplete="email"></label>',
                "Send reset code")
            return self.page("Forgot password?", '<section class="card narrow"><h1>Forgot password?</h1>'
                "<p>Enter your verified account email to request a reset code.</p>" + form +
                '<a href="/signin">Back to sign in</a></section>', request=request)
        data = await self.checked_form(request)
        email = data.get("email", "").strip().lower()
        token = request.cookies.get(RESET_COOKIE, "")
        try:
            self.validate_email_address(email)
        except ValueError:
            pass
        else:
            if self.allow_auth_attempt("forgot-password", digest(email)):
                pending = self.create_password_reset(email)
                if pending:
                    token, code, recipient = pending
                    if recipient:
                        self.queue_password_reset_mail(self.send_account_code, recipient, code, locale.get()[0],
                                                       "Your Ramzy Bots password reset code")
        if not COOKIE_TOKEN.fullmatch(token):
            token = secrets.token_urlsafe(32)
        response = web.HTTPSeeOther("/reset-password")
        response.set_cookie(RESET_COOKIE, token, max_age=CODE_AGE, secure=True,
                            httponly=True, samesite="Strict", path="/")
        raise response

    def password_reset_page(self, request, error=""):
        form = self.form(request, "/reset-password",
            '<label>Verification code<input name="code" inputmode="numeric" autocomplete="one-time-code" '
            'pattern="[0-9]{6}" maxlength="6" required></label>'
            '<label>New password<input type="password" name="new_password" minlength="12" maxlength="256" '
            'autocomplete="new-password" required></label>'
            '<label>Confirm new password<input type="password" name="confirm_password" minlength="12" maxlength="256" '
            'autocomplete="new-password" required></label>', "Reset password")
        return self.page("Reset password", '<section class="card narrow"><h1>Reset password</h1>'
            "<p>If the address can be used for recovery, you will receive a six-digit code.</p>"
            f"<p>{escape(error)}</p>" + form + '<p><a href="/forgot-password">Request a new code</a></p>'
            '<a href="/signin">Back to sign in</a></section>', status=400 if error else 200, request=request)

    def matching_password_reset(self, token, code):
        if not COOKIE_TOKEN.fullmatch(token):
            return None
        pending = self.one("SELECT * FROM password_resets WHERE token = ? AND expires > ? AND attempts < ?",
                           digest(token), int(time.time()), CODE_ATTEMPTS)
        if (pending and re.fullmatch(r"[0-9]{6}", code)
                and hmac.compare_digest(pending["code"], self.verification_code_digest(token, code))
                and pending["email"]):
            return pending
        return None

    async def reset_password(self, request):
        if request.method == "GET":
            return self.password_reset_page(request)
        data = await self.checked_form(request, max_length=8 * 1024)
        new = data.get("new_password", "")
        if not 12 <= len(new) <= 256:
            return self.password_reset_page(request, "Choose a new password of 12 to 256 characters.")
        if new != data.get("confirm_password", ""):
            return self.password_reset_page(request, "The new passwords do not match.")
        token = request.cookies.get(RESET_COOKIE, "")
        code = data.get("code", "").strip()
        invalid = "The reset code is invalid or expired."
        if not self.matching_password_reset(token, code):
            if COOKIE_TOKEN.fullmatch(token):
                self.db.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE token = ? AND attempts < ?",
                                (digest(token), CODE_ATTEMPTS))
            return self.password_reset_page(request, invalid)
        new_salt = secrets.token_bytes(16)
        async with self.auth_slots:
            new_hash = await asyncio.to_thread(password_hash, new, new_salt)
        async with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            with self.db:
                pending = self.matching_password_reset(token, code)
                if not pending:
                    return self.password_reset_page(request, invalid)
                email = pending["email"]
                self.db.execute("UPDATE accounts SET salt = ?, password = ? WHERE email = ?", (new_salt, new_hash, email))
                self.db.execute("DELETE FROM password_resets WHERE email = ?", (email,))
                self.db.execute("DELETE FROM sessions WHERE email = ?", (email,))
                self.db.execute("DELETE FROM pending_email_changes WHERE email = ?", (email,))
        language = locale.get()[0]
        self.queue_password_reset_mail(self.send_mail, email, translate("Your Ramzy Bots password was reset", language),
            translate("Your password was reset and your previous sessions were signed out.", language))
        response = web.HTTPSeeOther("/signin?reset=1")
        response.del_cookie(RESET_COOKIE, secure=True, httponly=True, samesite="Strict", path="/")
        response.del_cookie(COOKIE, secure=True, httponly=True, samesite="Lax", path="/")
        raise response

    def account_page(self, request, account, message="", status=200):
        csrf = self.csrf_token(request)
        name = escape(account["display_name"], quote=True)
        email = escape(account["email"])
        content = (f'<section class="card narrow"><h1>Account settings</h1><p>{escape(message)}</p>'
                   f'<p>Current email <bdi>{email}</bdi></p>')
        if not account["email_verified"]:
            content += "<p>Verify your current email to enable password recovery and account emails.</p>"
            content += self.form(request, "/account",
                '<input type="hidden" name="operation" value="verify-email">',
                "Verify current email", csrf=csrf)
        content += self.form(request, "/account",
            '<input type="hidden" name="operation" value="name">'
            f'<label>Display name<input name="name" value="{name}" maxlength="40" required autocomplete="nickname"></label>',
            "Save name", csrf=csrf)
        content += "<p>Changing your email requires a code from both addresses.</p>"
        content += self.form(request, "/account",
            '<input type="hidden" name="operation" value="email">'
            '<label>New email<input type="email" name="email" maxlength="254" required autocomplete="email"></label>',
            "Send code to current email", csrf=csrf)
        pending = self.one("""SELECT * FROM pending_email_changes WHERE email = ? AND session = ?
            AND expires > ? AND attempts < ?""", account["email"], digest(request.cookies[COOKIE]),
            int(time.time()), CODE_ATTEMPTS)
        if pending:
            if pending["stage"] in {"old", "verify"}:
                help_text, recipient = "Enter the code sent to your current email.", account["email"]
            else:
                help_text, recipient = "Enter the code sent to your new email.", pending["new_email"]
            content += f'<p>{help_text} <bdi>{escape(recipient)}</bdi></p>'
            content += self.form(request, "/account",
                '<input type="hidden" name="operation" value="confirm">'
                '<label>Verification code<input name="code" inputmode="numeric" autocomplete="one-time-code" '
                'pattern="[0-9]{6}" maxlength="6" required></label>', "Confirm code", csrf=csrf)
        content += "<h2>Change password</h2>"
        content += self.form(request, "/account",
            '<input type="hidden" name="operation" value="password">'
            '<label>Current password<input type="password" name="current_password" maxlength="256" '
            'autocomplete="current-password" required></label>'
            '<label>New password<input type="password" name="new_password" minlength="12" maxlength="256" '
            'autocomplete="new-password" required></label>'
            '<label>Confirm new password<input type="password" name="confirm_password" minlength="12" maxlength="256" '
            'autocomplete="new-password" required></label>', "Change password", csrf=csrf)
        content += self.form(request, "/signout", "", "Sign out", csrf=csrf) + "</section>"
        response = self.page("Account settings", content, status, request=request)
        response.set_cookie(CSRF_COOKIE, csrf, secure=True, httponly=True, samesite="Lax", path="/")
        return response

    async def account_settings(self, request):
        account = self.account(request)
        if not account:
            raise web.HTTPSeeOther("/signin")
        if request.method == "GET":
            messages = {"name": "Display name updated.", "email": "Email updated.", "password": "Password updated.",
                        "verified": "Email verified."}
            return self.account_page(request, account, messages.get(request.query.get("updated"), ""))
        # Three 256-character passwords can use over 9 KB after UTF-8 form encoding.
        data = await self.checked_form(request, max_length=10 * 1024)
        operation = data.get("operation")
        try:
            if operation == "name":
                name = self.validate_display_name(data.get("name", ""))
                async with self.lock:
                    account = self.account(request)
                    if not account:
                        raise web.HTTPSeeOther("/signin")
                    self.db.execute("UPDATE accounts SET display_name = ? WHERE email = ?", (name, account["email"]))
                raise web.HTTPSeeOther("/account?updated=name")
            if operation == "email":
                await self.request_email_change(request, data.get("email", "").strip().lower())
                raise web.HTTPSeeOther("/account")
            if operation == "verify-email":
                await self.request_email_change(request, None)
                raise web.HTTPSeeOther("/account")
            if operation == "password":
                if not self.allow_auth_attempt("password", account["email"]):
                    return self.account_page(request, account, "Try again in a minute.", status=429)
                new_token = await self.change_password(request, account, data)
                response = web.HTTPSeeOther("/account?updated=password")
                response.set_cookie(COOKIE, new_token, max_age=SESSION_AGE, secure=True,
                                    httponly=True, samesite="Lax", path="/")
                raise response
            if operation == "confirm":
                updated, new_token = await self.confirm_email_change(request, data.get("code", "").strip())
                response = web.HTTPSeeOther(f"/account?updated={updated}" if updated else "/account")
                if new_token:
                    response.set_cookie(COOKIE, new_token, max_age=SESSION_AGE, secure=True,
                                        httponly=True, samesite="Lax", path="/")
                raise response
        except ValueError as error:
            account = self.account(request)
            if not account:
                raise web.HTTPSeeOther("/signin") from None
            return self.account_page(request, account, str(error), status=400)
        raise web.HTTPBadRequest()

    async def change_password(self, request, account, data):
        current = data.get("current_password", "")
        new = data.get("new_password", "")
        if not 12 <= len(new) <= 256:
            raise ValueError("Choose a new password of 12 to 256 characters.")
        if new != data.get("confirm_password", ""):
            raise ValueError("The new passwords do not match.")
        if not 1 <= len(current) <= 256:
            raise ValueError("Current password is incorrect.")
        async with self.auth_slots:
            hashed = await asyncio.to_thread(password_hash, current, account["salt"])
            if not hmac.compare_digest(account["password"], hashed):
                raise ValueError("Current password is incorrect.")
            if current == new:
                raise ValueError("Choose a password different from your current password.")
            new_salt = secrets.token_bytes(16)
            new_hash = await asyncio.to_thread(password_hash, new, new_salt)
        email = account["email"]
        session = digest(request.cookies[COOKIE])
        async with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            with self.db:
                active = self.account(request)
                if not active or active["email"] != email or not hmac.compare_digest(active["password"], hashed):
                    raise web.HTTPSeeOther("/signin")
                self.db.execute("UPDATE accounts SET salt = ?, password = ? WHERE email = ?",
                                (new_salt, new_hash, email))
                self.db.execute("DELETE FROM pending_email_changes WHERE email = ?", (email,))
                self.db.execute("DELETE FROM password_resets WHERE email = ?", (email,))
                return self.rotate_account_session(email, session)

    async def request_email_change(self, request, new_email):
        verify_current = new_email is None
        if not verify_current:
            self.validate_email_address(new_email)
        async with self.lock:
            account = self.account(request)
            if not account:
                raise web.HTTPSeeOther("/signin")
            email = account["email"]
            if verify_current:
                if account["email_verified"]:
                    return
                self.validate_email_address(email)
                new_email = email
            elif new_email == email:
                raise ValueError("Enter a different email address.")
            elif self.one("SELECT 1 FROM accounts WHERE email = ?", new_email):
                raise ValueError("That email address is already in use.")
            now = int(time.time())
            pending = self.one("SELECT expires FROM pending_email_changes WHERE email = ?", email)
            if pending and pending["expires"] > now + CODE_AGE - 60:
                raise ValueError("Wait a minute before requesting another code.")
            token = request.cookies[COOKIE]
            code = f"{secrets.randbelow(1_000_000):06d}"
            hashed = self.verification_code_digest(token, code)
            self.db.execute("""INSERT INTO pending_email_changes(email, new_email, session, stage, code, expires)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET
                new_email = excluded.new_email, session = excluded.session, stage = excluded.stage,
                code = excluded.code, expires = excluded.expires, attempts = 0""",
                (email, new_email, digest(token), "verify" if verify_current else "old", hashed, now + CODE_AGE))
        subject = "Your Ramzy Bots verification code" if verify_current else "Your Ramzy Bots email change code"
        await self.send_email_change_code(email, email, code, hashed, subject)

    async def send_email_change_code(self, account_email, recipient, code, hashed,
                                     subject="Your Ramzy Bots email change code"):
        try:
            await asyncio.to_thread(self.send_account_code, recipient, code, locale.get()[0], subject)
        except (OSError, ValueError, KeyError) as error:
            LOG.warning("Email change code delivery failed (%s)", type(error).__name__)
            self.db.execute("DELETE FROM pending_email_changes WHERE email = ? AND code = ?", (account_email, hashed))
            raise ValueError("We couldn't send a code right now. Try again later.") from None

    async def confirm_email_change(self, request, code):
        async with self.lock:
            account = self.account(request)
            if not account:
                raise web.HTTPSeeOther("/signin")
            email = account["email"]
            token = request.cookies[COOKIE]
            session = digest(token)
            now = int(time.time())
            pending = self.one("""SELECT * FROM pending_email_changes WHERE email = ? AND session = ?
                AND expires > ? AND attempts < ?""", email, session, now, CODE_ATTEMPTS)
            if not pending:
                raise ValueError("This code has expired or is no longer available. Request a new code.")
            if not re.fullmatch(r"[0-9]{6}", code) or not hmac.compare_digest(
                    pending["code"], self.verification_code_digest(token, code)):
                self.db.execute("UPDATE pending_email_changes SET attempts = attempts + 1 WHERE email = ?", (email,))
                if pending["attempts"] + 1 == CODE_ATTEMPTS:
                    raise ValueError("This code has expired or is no longer available. Request a new code.")
                raise ValueError("That code is incorrect. Try again.")
            new_email = pending["new_email"]
            if pending["stage"] == "verify":
                self.db.execute("BEGIN IMMEDIATE")
                with self.db:
                    self.db.execute("UPDATE accounts SET email_verified = 1 WHERE email = ?", (email,))
                    self.db.execute("DELETE FROM pending_email_changes WHERE email = ?", (email,))
                return "verified", None
            if pending["stage"] == "new":
                return "email", self.change_email(email, new_email, session)
            next_code = f"{(int(code) + 1 + secrets.randbelow(999_999)) % 1_000_000:06d}"
            hashed = self.verification_code_digest(token, next_code)
            self.db.execute("""UPDATE pending_email_changes SET stage = 'new', code = ?, expires = ?, attempts = 0
                WHERE email = ?""", (hashed, now + CODE_AGE, email))
        await self.send_email_change_code(email, new_email, next_code, hashed)
        return None, None

    def change_email(self, email, new_email, session):
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            if self.one("SELECT 1 FROM accounts WHERE email = ?", new_email):
                raise ValueError("That email address is already in use.")
            self.db.execute("PRAGMA defer_foreign_keys = ON")
            self.db.execute("DELETE FROM pending_email_changes WHERE email = ?", (email,))
            self.db.execute("DELETE FROM password_resets WHERE email = ?", (email,))
            self.db.execute("UPDATE accounts SET email = ?, email_verified = 1 WHERE email = ?", (new_email, email))
            self.db.execute("UPDATE rooms SET customer = ? WHERE customer = ?", (new_email, email))
            self.db.execute("UPDATE grants SET email = ? WHERE email = ?", (new_email, email))
            self.db.execute("UPDATE site_admins SET email = ? WHERE email = ?", (new_email, email))
            return self.rotate_account_session(email, session, new_email=new_email)

    def rotate_account_session(self, email, session, *, new_email=None):
        new_token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM sessions WHERE email = ? AND token != ?", (email, session))
        self.db.execute("UPDATE sessions SET email = ?, token = ?, expires = ? WHERE token = ?",
                        (new_email or email, digest(new_token), int(time.time()) + SESSION_AGE, session))
        return new_token

    @staticmethod
    async def checked_form(request, *, max_length=4096):
        if request.content_type != "application/x-www-form-urlencoded" or request.content_length is None or request.content_length > max_length:
            raise web.HTTPBadRequest()
        data = await request.post()
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not COOKIE_TOKEN.fullmatch(cookie) or not hmac.compare_digest(cookie.encode(), data.get("csrf", "").encode()):
            raise web.HTTPForbidden(text="Reload the page and try again.")
        return data

    async def home(self, request):
        eyebrow = "PRIVATE BETA FOR IMVU ROOM HOSTS" if self.invite_only else "FOR IMVU ROOM HOSTS"
        if self.account(request):
            account_links = ('<a class="primary" href="/panel">Open control panel</a>'
                             '<a href="/account">Account settings</a>')
        else:
            account_links = '<a class="primary" href="/signup">Create account</a>'
        return self.page("IMVU room companion", """
            <section class="hero"><div class="hero-copy"><p class="eyebrow">""" + eyebrow + """</p>
            <h1>Turn up your room.</h1>
            <p>Play music, welcome guests, and start games in your IMVU room. Manage it all from Ramzy Bots.</p>
            <div class="actions">""" + account_links + """<a class="secondary" href="/guide">Read the command guide</a></div></div>
            <div class="signal" aria-hidden="true" dir="ltr">
                <div class="signal-header"><span>RAMZY BOTS / IMVU</span><i></i></div>
                <svg class="signal-art" viewBox="0 0 420 370" fill="none" aria-hidden="true">
                    <defs>
                        <radialGradient id="signal-core"><stop stop-color="#ff374d" stop-opacity=".28"/><stop offset="1" stop-color="#920b25" stop-opacity="0"/></radialGradient>
                        <linearGradient id="signal-stroke" x1="105" y1="70" x2="310" y2="293" gradientUnits="userSpaceOnUse">
                            <stop stop-color="#ff8994"/><stop offset=".45" stop-color="#ff334f"/><stop offset="1" stop-color="#740c23"/>
                        </linearGradient>
                    </defs>
                    <circle cx="210" cy="180" r="158" fill="url(#signal-core)"/>
                    <path d="M210 22v316M52 180h316M99 69l222 222M99 291 321 69" stroke="#742033" stroke-opacity=".45"/>
                    <circle cx="210" cy="180" r="151" stroke="#6d2435" stroke-dasharray="1 7"/>
                    <circle cx="210" cy="180" r="133" stroke="#5e1728"/>
                    <circle cx="210" cy="180" r="109" stroke="#7e2339" stroke-dasharray="5 8"/>
                    <circle cx="210" cy="180" r="76" stroke="#59202d"/>
                    <g class="signal-orbit">
                        <path d="M210 47a133 133 0 0 1 126 91M210 313a133 133 0 0 1-126-91" stroke="url(#signal-stroke)" stroke-width="3" stroke-linecap="round"/>
                        <circle cx="336" cy="138" r="5" fill="#ff586f"/>
                        <circle cx="84" cy="222" r="3" fill="#ff586f"/>
                    </g>
                    <path d="m210 112 60 34v68l-60 35-60-35v-68z" fill="#10080c" stroke="url(#signal-stroke)" stroke-width="2"/>
                    <g class="signal-bars" stroke="url(#signal-stroke)" stroke-width="7" stroke-linecap="round">
                        <path d="M178 169v22"/><path d="M194 156v48"/><path d="M210 145v70"/><path d="M226 159v42"/><path d="M242 170v20"/>
                    </g>
                </svg>
                <div class="signal-footer"><span>01 / !radio</span><span>02 / !play</span><span>03 / !game</span></div>
            </div></section>
            <section class="grid feature-grid">
            <article class="card"><span class="feature-index" aria-hidden="true">01 / AUDIO</span><h2>Room radio</h2><p>Get your room’s MP3 link with <code>!radio</code>. Guests use <code>!play</code> to request tracks and <code>!queue</code> to see what’s next.</p></article>
            <article class="card"><span class="feature-index" aria-hidden="true">02 / CHAT</span><h2>Welcomes and games</h2><p>Write a greeting using <code>{name}</code> for the visitor and <code>{room}</code> for the room. Guests can start trivia with <code>!game</code> or a number round with <code>!guess</code>.</p></article>
            <article class="card"><span class="feature-index" aria-hidden="true">03 / ACCESS</span><h2>Room controls</h2><p>See connection status, choose the reply language, and switch welcomes or quiet mode on or off. The service owner assigns website admins separately for each room.</p></article>
            </section>
            <section class="card onboarding"><h2>How to get a room connected</h2><ol>
            <li>Create an account, verify your email, and sign in.</li>
            <li>Enter your IMVU room ID or link in the control panel.</li>
            <li>Start your free 3-day trial. It begins when the bot connects.</li>
            </ol><h3>Continue with VCOIN</h3>
            <p>700 VCOIN for 30 days or 6,720 VCOIN for one year. The yearly plan saves 20%.</p>
            <p>The bot leaves when access ends and reconnects after a renewal is confirmed.</p></section>
            """, request=request)

    async def guide(self, request):
        return self.page("Command guide", """
            <section class="panel-head"><p class="eyebrow">RAMZY BOTS / IMVU</p>
            <h1>Command guide</h1><p>Type these commands in an IMVU room where the bot is connected. Use <code>!help</code> for a quick list in chat.</p></section>
            <section class="grid">
            <article class="card"><h2>Set up the room radio</h2><ol>
            <li>Type <code>!radio</code> in the room to get its MP3 link and listener count.</li>
            <li>In IMVU Classic, the room host or moderator opens Radio, loads that link, and presses Play.</li>
            <li>Type <code>!play artist or song</code> in room chat. If a numbered list appears, reply with the number within two minutes. You can also submit a supported HTTPS track link.</li>
            </ol><p>Each room has its own stream and queue. A song request adds a track to the queue.</p>
            <p><code>!queue</code> shows upcoming songs, <code>!np</code> shows the current track, <code>!skip</code> advances playback, and <code>!stop</code> stops playback and clears the queue. Owner-requested tracks have skip and stop protection.</p></article>
            <article class="card"><h2>Play in chat</h2>
            <p><code>!game</code> starts trivia or a riddle. Answer in room chat or use <code>!answer your answer</code>.</p>
            <p><code>!guess</code> starts a round from 1 to 30. The bot gives higher or lower hints. <code>!scores</code> shows your points for the current session.</p>
            <p>Try <code>!joke</code>, <code>!dare</code>, or <code>!fortune</code> for a quick room prompt.</p></article>
            <article class="card"><h2>Chat, move, and request an invite</h2>
            <p>Use <code>!ask your question</code> for a reply. Choose the bot’s reply language in your room settings.</p>
            <p><code>!move</code> asks the bot to move its avatar to a free seat near you, when the furniture has a suitable seat.</p>
            <p>Send <code>!invite room ID</code> in a new one-to-one IMVU message to the bot for an invitation to a room it has joined. IMVU applies the room’s admission and capacity rules.</p></article>
            <article class="card"><h2>Manage your room</h2>
            <p>Choose the bot’s language from the room’s Language menu in the control panel.</p>
            <p>Use <code>{name}</code> and <code>{room}</code> in a welcome of up to 300 characters. Turn on Quiet mode to limit conversational follow-ups while keeping direct requests and commands.</p>
            <p>Website room admins can change settings only in assigned rooms. Room access follows the trial or subscription.</p>
            </article></section>""", request=request)

    @staticmethod
    def vcoin_plan_fields():
        return ('<label>Subscription plan<select name="plan" required>'
                '<option value="monthly">30 days · 700 VCOIN</option>'
                '<option value="yearly">1 year · 6,720 VCOIN · save 20%</option></select></label>')

    def room_display_name(self, room, entry, *, remember=False):
        saved_name = self.one("SELECT name FROM room_names WHERE room = ?", room)
        live_name = getattr(entry[0], "room_name", room) if entry else room
        if not live_name or live_name == room:
            return saved_name["name"] if saved_name else room
        if remember and (not saved_name or saved_name["name"] != live_name):
            self.db.execute("""INSERT INTO room_names VALUES (?, ?)
                ON CONFLICT(room) DO UPDATE SET name = excluded.name""", (room, live_name))
        return live_name

    @staticmethod
    def room_status_label(status, access, expired, entry):
        if expired:
            return "Expired"
        if status != "approved":
            return status.title()
        if Portal.trial_waiting(access):
            return "Preparing trial" if access["join_pending"] else "Trial paused"
        if entry and not entry[1].done():
            bot = entry[0]
            if getattr(bot, "status", "") in {"verification_required", "rate_limited", "login_rejected", "login_paused"}:
                return "Offline"
            return "Connected" if bot.connected and bot.chat_ready else "Connecting"
        return "Connecting" if access and access["join_pending"] else "Offline"

    def room_card(self, request, row, owner, moderator=False, customer=False):
        room = row["id"]
        status = row["status"]
        access = self.room_access(room)
        term = self.one("SELECT expires FROM owner_room_terms WHERE room = ?", room)
        expired = status == "approved" and not self.room_can_join(room)
        trial_waiting = self.trial_waiting(access)
        usable = status == "approved" and not expired and not trial_waiting
        entry = self.manager.rooms.get(room)
        health = self.one("SELECT last_connected, disconnect_reason FROM room_health WHERE room = ?", room)
        room_name = self.room_display_name(room, entry, remember=True)
        label = self.room_status_label(status, access, expired, entry)
        content = f'<article class="card"><p class="eyebrow">{escape(label)}</p><h2><span>Room</span> <bdi>{escape(room_name)}</bdi></h2>'
        last_connected = "<span>No connection recorded yet</span>"
        reason = health["disconnect_reason"] if health else None
        if health and health["last_connected"] is not None:
            connected_at = datetime.fromtimestamp(health["last_connected"], timezone.utc)
            last_connected = f'<bdi><time datetime="{connected_at.isoformat()}">{connected_at:%Y-%m-%d %H:%M} UTC</time></bdi>'
        if status == "suspended":
            reason = "access_suspended"
        elif expired:
            reason = "access_expired"
        explanation = ROOM_HEALTH_REASONS.get(reason, "No disconnect recorded yet")
        content += ('<h3>Room health</h3>'
                    f'<p><span>Last connected</span> · {last_connected}</p>'
                    f'<p><span>Last disconnect reason</span> · <span>{explanation}</span></p>')
        hidden = f'<input type="hidden" name="room" value="{escape(room, quote=True)}">'
        if term:
            if term["expires"] is None:
                content += "<p>No expiration</p>"
            else:
                last_day = (datetime.fromtimestamp(term["expires"], timezone.utc).date() - timedelta(days=1)).isoformat()
                content += f'<p><span>Access through (UTC)</span> <time>{last_day}</time></p>'
        if owner and status == "approved":
            if room != self.manager.default_room:
                content += self.form(request, "/owner/set-expiration", hidden +
                    '<label>Last day of access (UTC)<input type="date" name="end_date" required></label>', "Set expiration")
            content += self.form(request, "/owner/no-expiration", hidden, "Set no expiration")
        if access and not term:
            if access["unlimited"]:
                content += "<p>No expiration</p>"
            elif trial_waiting:
                content += "<p>Your trial starts when the bot connects. Make sure it can enter your room.</p>"
            elif access["expires"]:
                label_text = "Expired on" if expired else "Active until"
                if usable and access["trial_requested"] and not self.one(
                        "SELECT 1 FROM vcoin_orders WHERE room = ? AND transfer IS NOT NULL", room):
                    label_text = "Trial ends"
                end = datetime.fromtimestamp(access["expires"], timezone.utc)
                content += f'<p><span>{label_text}</span> <time datetime="{end.isoformat()}">{end:%Y-%m-%d %H:%M} UTC</time></p>'
        if owner and access and access["unlimited"]:
            content += self.form(request, "/owner/revoke-unlimited", hidden, "Revoke no-expiration access")
        if customer or owner:
            content += self.room_order_card(request, room, hidden, owner, customer, status, access, term)
        if usable:
            prefs = self.manager.settings.get(room)
            quiet = " checked" if prefs["quiet"] else ""
            welcomes = " checked" if prefs["welcomes"] else ""
            language = self.manager.room_languages.get(room, "auto")
            welcome = escape(prefs["welcome"], quote=True)
            preview = escape(welcome_message(language, prefs["welcome"], "Guest", room_name)[:450])
            if label == "Connected":
                content += f'<p><a href="/rooms/{escape(room, quote=True)}/stream.mp3">Room radio stream</a> · <a href="/guide">Radio setup</a></p>'
            content += self.form(request, "/language", hidden +
                f'<label>Bot language<select name="language">{room_language_options(language)}</select></label>', "Save language")
            preview_label = "Custom welcome preview" if prefs["welcome"] else "Default welcome preview"
            content += f'<h3>{preview_label}</h3><blockquote dir="auto" translate="no">{preview}</blockquote>'
            if not prefs["welcomes"]:
                content += '<p>Welcomes are off for this room.</p>'
            content += self.form(request, "/settings", hidden +
                f'<label>Welcome message<input name="welcome" value="{welcome}" maxlength="300"></label>'
                '<small>Leave blank to use the default. Use {name} for the visitor and {room} for the room.</small>'
                f'<label class="check"><input type="checkbox" name="welcomes"{welcomes}> Welcome guests</label>'
                f'<label class="check"><input type="checkbox" name="quiet"{quiet}> Quiet mode</label>'
                '<details><summary>What Quiet mode does</summary>'
                '<p>The bot responds to direct mentions and chat commands. It skips unaddressed conversation follow-ups. '
                'Some plain-chat game answers can also be skipped, so use the answer command for games. '
                'Music playback and automatic welcomes have their own controls.</p></details>', "Save settings")
        if (owner or moderator) and (usable or (status == "approved" and trial_waiting and not expired)):
            content += self.form(request, "/owner/join", hidden, "Connect bot")
            if room != self.manager.default_room:
                content += self.form(request, "/owner/leave", hidden, "Disconnect bot")
        if owner:
            linked = self.one("SELECT owner FROM accounts WHERE email = ?", row["customer"])
            content += '<p>No customer assigned</p>' if linked["owner"] else f'<p>Customer · {escape(row["customer"])}</p>'
            if status == "suspended":
                content += self.form(request, "/owner/approve", hidden, "Restore access")
            elif status == "approved":
                if room != self.manager.default_room:
                    content += self.form(request, "/owner/suspend", hidden, "Suspend access")
            if room != self.manager.default_room:
                content += ('<details><summary>Remove room</summary>'
                            '<p>This disconnects the bot and permanently deletes this room’s saved settings, access and orders.</p>'
                            + self.form(request, "/owner/remove-room", hidden +
                                '<label>Type the room ID to confirm<input name="confirm_room" maxlength="300" required></label>'
                                f'<p><code>{escape(room)}</code></p>', "Remove room permanently") + "</details>")
            if status == "approved":
                content += '<h3>Room admins</h3>'
                for grant in self.db.execute("SELECT email FROM grants WHERE room = ? ORDER BY email", (room,)):
                    email = escape(grant["email"], quote=True)
                    content += self.form(request, "/owner/revoke", hidden +
                        f'<input type="hidden" name="email" value="{email}"><p>{email}</p>', "Remove admin")
                content += self.form(request, "/owner/grant", hidden +
                    '<label>Admin account email<input type="email" name="email" maxlength="254" required></label>', "Add room admin")
        return content + "</article>"

    def room_order_card(self, request, room, hidden, owner, customer, status, access, term):
        orders = self.db.execute("""SELECT * FROM vcoin_orders
            WHERE room = ? AND transfer IS NULL AND (? OR payer IS NULL) ORDER BY rowid""", (room, owner)).fetchall()
        if not orders:
            if (customer and status != "suspended" and (self.billing_test or self.vcoin_recipient)
                    and room != self.manager.default_room
                    and not (access and access["unlimited"]) and not (term and term["expires"] is None)):
                return self.form(request, "/vcoin-order", hidden + self.vcoin_plan_fields(), "Create VCOIN order")
            return ""
        return "".join(self.vcoin_order_details(request, order, status, owner=owner, payer=customer)
                       for order in orders)

    def vcoin_order_details(self, request, order, status, *, owner=False, payer=False, from_inbox=False):
        order_id = escape(order["id"], quote=True)
        room = escape(order["room"], quote=True)
        fields = (f'<input type="hidden" name="room" value="{room}">'
                  f'<input type="hidden" name="order" value="{order_id}">')
        content = f"<p><span>VCOIN order</span> <code>{order_id}</code> · {order['amount']:,} VCOIN</p>"
        if order["reported_sender"]:
            content += ('<p><span>Reported by IMVU user</span> '
                        f'<bdi>{escape(order["reported_sender"])}</bdi></p><p>Awaiting wallet verification</p>')
        if (payer and not self.billing_test and self.vcoin_recipient and status != "suspended"
                and not order["reported_sender"]):
            recipient = escape(self.vcoin_recipient)
            content += ('<p><span>Send the exact order amount in IMVU to</span> '
                        f'<bdi>{recipient}</bdi><span>. Include this order code in the transfer note.</span></p>'
                        '<p>IMVU deducts a 5% transfer fee from the amount sent.</p>'
                        '<p><a href="https://www.imvu.com/next/gift/" target="_blank" rel="noopener noreferrer">Open VCOIN transfer in IMVU</a></p>')
            content += self.form(request, "/vcoin-report", fields +
                '<label>Your sending IMVU username<input name="sender" maxlength="64" required></label>', "Report transfer")
            content += self.form(request, "/vcoin-cancel", fields, "Cancel unpaid order")
        if owner and status != "suspended" and (self.billing_test or order["reported_sender"]):
            if from_inbox:
                fields += '<input type="hidden" name="from_inbox" value="1">'
            confirmation = ""
            if not self.billing_test:
                net = f"{order['amount'] * 0.95:,.2f}".rstrip("0").rstrip(".")
                confirmation = (f'<p><span>Expected receipt after IMVU’s fee</span> <bdi>{net} VCOIN</bdi></p>'
                                '<label class="check"><input type="checkbox" name="verified" value="1" required> '
                                'I matched the sender, amount, and order code in my IMVU Wallet.</label>')
            content += self.form(request, "/owner/confirm-vcoin", fields + confirmation +
                '<label>Wallet transaction reference<input name="transfer" maxlength="80" required></label>',
                "Confirm VCOIN transfer")
            if order["reported_sender"]:
                content += self.form(request, "/owner/return-vcoin-report", fields, "Return transfer report for correction")
        return content

    async def payment_inbox(self, request):
        account = self.account(request)
        if not account:
            raise web.HTTPSeeOther("/signin")
        if not account["owner"]:
            raise web.HTTPForbidden()

        content = ('<section class="panel-head"><p class="eyebrow">RAMZY BOTS / DASHBOARD</p>'
                   '<h1>VCOIN payment inbox</h1><p><a href="/owner">Admin panel</a></p></section>')
        if self.billing_test:
            content += '<p><strong>VCOIN test mode.</strong> Use simulated transfers only.</p>'
        reports = self.db.execute("""SELECT v.*, r.status, n.name FROM vcoin_orders v
            JOIN rooms r ON r.id = v.room LEFT JOIN room_names n ON n.room = r.id
            WHERE v.transfer IS NULL AND v.reported_sender IS NOT NULL AND v.room <> ?
            ORDER BY v.rowid""", (self.manager.default_room,)).fetchall()
        if not reports:
            content += '<section class="card"><p>No reported VCOIN transfers are waiting for review.</p></section>'
        else:
            cards = []
            for report in reports:
                room = escape(report["room"])
                room_name = escape(report["name"] or report["room"])
                card = f'<article class="card"><h2><span>Room</span> <bdi>{room_name}</bdi></h2>'
                if room_name != room:
                    card += f'<p><span>IMVU room ID</span> <bdi>{room}</bdi></p>'
                if report["status"] == "suspended":
                    card += "<p>Restore this room before confirming its payment.</p>"
                card += self.vcoin_order_details(request, report, report["status"], owner=True, from_inbox=True)
                cards.append(card + "</article>")
            content += '<section class="grid">' + "".join(cards) + "</section>"
        return self.page("VCOIN payment inbox", content, request=request)

    def existing_payment_orders(self, request, account):
        # Keep previously created payment orders accessible until they are settled or canceled.
        orders = self.db.execute("""SELECT v.*, r.status FROM vcoin_orders v JOIN rooms r ON r.id = v.room
            WHERE v.payer = ? AND v.transfer IS NULL ORDER BY v.rowid DESC""", (account["email"],)).fetchall()
        if not orders:
            return ""
        cards = "".join(f'<article class="card"><h3><span>Room</span> <bdi>{escape(order["room"])}</bdi></h3>'
                        + self.vcoin_order_details(request, order, order["status"], payer=True) + "</article>"
                        for order in orders)
        return '<section class="grid"><h2>Existing VCOIN orders</h2>' + cards + "</section>"

    @staticmethod
    def payment_date(timestamp):
        if timestamp is None:
            return "Date not recorded"
        when = datetime.fromtimestamp(timestamp, timezone.utc)
        return f'<time datetime="{when.isoformat()}">{when:%Y-%m-%d %H:%M} UTC</time>'

    async def payment_history(self, request):
        account = self.account(request)
        if not account:
            raise web.HTTPSeeOther("/signin")
        page = request.query.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,5}", page):
            raise web.HTTPNotFound()
        page_number = int(page)
        page_size = 24
        orders = self.db.execute("""SELECT v.id, v.room, n.name, v.amount,
            v.transfer IS NOT NULL AS confirmed, v.confirmed_at,
            COALESCE(v.renewed_until, receipt.expires) AS renewed_until
            FROM vcoin_orders v JOIN rooms r ON r.id = v.room LEFT JOIN room_names n ON n.room = v.room
            LEFT JOIN billing_email_outbox receipt ON receipt.kind = 'receipt'
                AND receipt.room = v.room AND receipt.event = v.id
            WHERE COALESCE(v.payer, r.customer) = ? AND (v.transfer IS NOT NULL OR v.reported_sender IS NOT NULL)
            ORDER BY v.rowid DESC LIMIT ? OFFSET ?""",
            (account["email"], page_size + 1, (page_number - 1) * page_size)).fetchall()
        if page_number > 1 and not orders:
            raise web.HTTPNotFound()
        content = ('<section class="panel-head"><p class="eyebrow">RAMZY BOTS / DASHBOARD</p>'
                   '<h1>Payment history</h1><p><a href="/panel">Control panel</a></p></section>')
        if self.billing_test:
            content += '<p><strong>VCOIN test mode.</strong> Use simulated transfers only.</p>'
        if not orders:
            content += '<section class="card"><p>No reported or confirmed payments yet.</p></section>'
        else:
            cards = []
            for order in orders[:page_size]:
                room = escape(order["room"])
                name = escape(order["name"] or order["room"])
                status = "Payment confirmed" if order["confirmed"] else "Awaiting wallet verification"
                card = f'<article class="card"><h2><span>Room</span> <bdi>{name}</bdi></h2>'
                if name != room:
                    card += f'<p><span>IMVU room ID</span> <bdi>{room}</bdi></p>'
                card += (f'<p><span>VCOIN order</span> <code>{escape(order["id"])}</code> · '
                         f'<bdi>{order["amount"]:,} VCOIN</bdi></p><p>{status}</p>')
                if order["confirmed"]:
                    renewed_until = order["renewed_until"]
                    renewal = "No expiration" if renewed_until == 0 else self.payment_date(renewed_until)
                    card += (f'<p><span>Confirmed on</span> <span>{self.payment_date(order["confirmed_at"])}</span></p>'
                             f'<p><span>Access renewed until</span> <span>{renewal}</span></p>')
                cards.append(card + "</article>")
            content += '<section class="grid">' + "".join(cards) + "</section>"
        if page_number > 1:
            content += f'<p><a href="/payments?page={page_number - 1}">Newer payments</a></p>'
        if len(orders) > page_size:
            content += f'<p><a href="/payments?page={page_number + 1}">Older payments</a></p>'
        return self.page("Payment history", content, request=request)

    async def panel(self, request):
        account = self.account(request)
        if not account:
            raise web.HTTPSeeOther("/signin")
        admin_panel = request.path == "/owner"
        owner = admin_panel and bool(account["owner"])
        moderator = admin_panel and not owner and self.site_admin(account)
        if admin_panel and not (owner or moderator):
            raise web.HTTPForbidden()
        if admin_panel:
            if not self.billing_test:
                self.import_owner_rooms()
        if owner:
            rows = self.db.execute("SELECT * FROM rooms ORDER BY id").fetchall()
        elif moderator:
            rows = self.db.execute("SELECT * FROM rooms WHERE status = 'approved' ORDER BY id").fetchall()
        else:
            rows = self.db.execute("""SELECT * FROM rooms r WHERE customer = ? OR
                (status = 'approved' AND EXISTS (SELECT 1 FROM grants g WHERE g.room = r.id AND g.email = ?))
                ORDER BY id""", (account["email"], account["email"])).fetchall()
        title = "Admin panel" if admin_panel else "Control panel"
        content = f'<section class="panel-head"><p class="eyebrow">RAMZY BOTS / DASHBOARD</p><h1>{title}</h1>'
        if self.site_admin(account):
            content += '<p><a href="/owner">Admin panel</a> · <a href="/panel">Customer view</a></p>'
        if admin_panel:
            content += '<p><a href="#room-search">Find rooms</a></p>'
        if account["owner"]:
            content += ('<p><a href="/owner/payments">VCOIN payment inbox</a>'
                        ' · <a href="/owner#recent-activity">Recent activity</a></p>')
        if not admin_panel:
            content += '<p><a href="/payments">Payment history</a></p>'
        content += self.form(request, "/signout", "", "Sign out") + "</section>"
        if owner:
            content += self.recent_activity()
        if self.billing_test:
            content += '<p><strong>VCOIN test mode.</strong> Use simulated transfers only.</p>'
        if not admin_panel:
            room_field = '<label>IMVU room ID or link<input name="room" maxlength="300" required></label>'
            content += ('<section class="card"><h2>Free 3-day trial</h2>'
                        '<p>One trial per account. It starts when the bot connects to your room.</p>' +
                        self.form(request, "/request-room", room_field, "Start trial") + "</section>")
            content += '<section class="card"><h2>VCOIN subscriptions</h2>'
            if self.billing_test or self.vcoin_recipient:
                content += '<p>Choose a room and report your transfer. Once the site owner confirms payment, you can manage that room from this panel.</p>'
                content += self.form(request, "/vcoin-order", room_field + self.vcoin_plan_fields(), "Create VCOIN order")
            else:
                content += '<p>VCOIN checkout is not available yet.</p>'
            content += "</section>"
            content += self.existing_payment_orders(request, account)
        elif owner:
            content += '<section class="card"><h2>Grant no-expiration access</h2>' + self.form(
                request, "/owner/grant-unlimited",
                '<label>Registered customer email<input type="email" name="email" maxlength="254" required></label>'
                '<label>IMVU room ID or link<input name="room" maxlength="300" required></label>',
                "Grant no-expiration access") + "</section>"
        if owner:
            content += ('<section class="card"><h2>Website moderators</h2>'
                        '<p>Moderators can manage settings and connect or disconnect the bot in approved rooms. '
                        'Only the owner can assign access or change expiration dates.</p>')
            candidates = self.db.execute("""SELECT email FROM accounts WHERE owner = 0
                AND NOT EXISTS (SELECT 1 FROM site_admins WHERE site_admins.email = accounts.email)
                ORDER BY email""").fetchall()
            if candidates:
                options = "".join(f'<option>{escape(row["email"])}</option>' for row in candidates)
                content += self.form(request, "/owner/add-site-admin",
                    '<label>Registered account email<select name="email" required>'
                    '<option value="" disabled selected>Select an account</option>' + options + '</select></label>',
                    "Add website moderator")
            else:
                content += '<p>No other registered accounts to add.</p>'
            for grant in self.db.execute("SELECT email FROM site_admins ORDER BY email"):
                email = escape(grant["email"], quote=True)
                content += self.form(request, "/owner/remove-site-admin",
                    f'<input type="hidden" name="email" value="{email}"><p>{email}</p>', "Remove website moderator")
            content += "</section>"
        empty = "No rooms yet."
        if admin_panel:
            filters, rows = self.admin_room_filters(request, rows, owner)
            content += filters
            empty = "No rooms match these filters."
        content += '<section class="grid">' + ("".join(self.room_card(
            request, row, owner, moderator, not admin_panel and row["customer"] == account["email"]) for row in rows)
            or f"<p>{empty}</p>") + "</section>"
        return self.page(title, content, request=request)

    def admin_room_filters(self, request, rows, owner):
        states = {
            "": "All statuses", "connected": "Connected", "connecting": "Connecting", "offline": "Offline",
            "expired": "Expired", "preparing-trial": "Preparing trial", "trial-paused": "Trial paused",
        }
        if owner:
            states.update({"pending": "Pending", "suspended": "Suspended"})
        state = request.query.get("status", "")
        if state not in states:
            state = ""
        query = request.query.get("q", "")[:300].strip()
        needle = query.casefold()
        matches = []
        for row in rows:
            room = row["id"]
            entry = self.manager.rooms.get(room)
            if needle and not (needle in room.casefold() or
                               (owner and needle in row["customer"].casefold()) or
                               needle in self.room_display_name(room, entry).casefold()):
                continue
            if state:
                expired = row["status"] == "approved" and not self.room_can_join(room)
                label = self.room_status_label(row["status"], self.room_access(room), expired, entry)
                if label != states[state]:
                    continue
            matches.append(row)
        options = "".join(f'<option value="{key}"{" selected" if key == state else ""}>{label}</option>'
                          for key, label in states.items())
        fields = "Room name, ID, or customer email" if owner else "Room name or ID"
        content = ('<section class="card" id="room-search"><h2>Find rooms</h2>'
                   '<form method="get" action="/owner#room-search">'
                   f'<label>{fields}<input type="search" name="q" value="{escape(query, quote=True)}" maxlength="300"></label>'
                   f'<label>Room status<select name="status">{options}</select></label>'
                   '<button>Apply filters</button><a href="/owner#room-search">Clear filters</a></form>'
                   f'<p><span>Matching rooms</span> · <bdi>{len(matches)} / {len(rows)}</bdi></p></section>')
        return content, matches

    def recent_activity(self):
        content = '<section class="card" id="recent-activity"><h2>Recent activity</h2>'
        events = self.db.execute("SELECT created, actor, action, room FROM audit ORDER BY rowid DESC LIMIT 25").fetchall()
        if not events:
            return content + "<p>No activity recorded yet.</p></section>"
        entries = []
        for event in events:
            action = "Room removed" if event["action"] == "remove-room" else event["action"]
            entries.append(f'<li><bdi>{escape(event["created"])} UTC</bdi> · <bdi>{escape(event["actor"])}</bdi>'
                           f' · <span>{escape(action)}</span> · <bdi>{escape(event["room"])}</bdi></li>')
        return content + "<ul>" + "".join(entries) + "</ul></section>"

    async def action(self, request):
        data = await self.checked_form(request)
        async with self.lock:
            account = self.account(request)
            if not account:
                raise web.HTTPSeeOther("/signin")
            action = request.path.rsplit("/", 1)[-1]
            owner_route = request.path.startswith("/owner/")
            if owner_route and not (account["owner"] or (action in {"join", "leave"} and self.site_admin(account))):
                raise web.HTTPForbidden()
            if action == "signout":
                self.db.execute("DELETE FROM sessions WHERE token = ?", (digest(request.cookies[COOKIE]),))
                response = web.HTTPSeeOther("/")
                response.set_cookie(COOKIE, "", max_age=0, secure=True, httponly=True, samesite="Lax", path="/")
                raise response
            back = "/owner" if owner_route else "/panel"
            if owner_route and action in {"confirm-vcoin", "return-vcoin-report"} and data.get("from_inbox") == "1":
                back = "/owner/payments"
            try:
                if action in {"add-site-admin", "remove-site-admin"}:
                    email = data.get("email", "").strip().lower()
                    if action == "add-site-admin":
                        if not self.one("SELECT email FROM accounts WHERE email = ? AND owner = 0", email):
                            raise ValueError("Enter the email of a registered account.")
                        self.db.execute("INSERT OR IGNORE INTO site_admins VALUES (?)", (email,))
                    else:
                        self.db.execute("DELETE FROM site_admins WHERE email = ?", (email,))
                    self.db.execute("INSERT INTO audit(actor, room, action) VALUES (?, ?, ?)",
                                    (account["email"], "Website", action + " " + email))
                    raise web.HTTPSeeOther(back)
                room = room_id(data.get("room", ""))
                await self.room_action(account, action, room, data)
            except ValueError as error:
                title = "Payment update" if "vcoin" in action else "Admin update" if "site-admin" in action else "Room update"
                return self.page(title, f'<section class="card"><h1>{title}</h1><p>{escape(str(error))}</p>'
                                 f'<a href="{back}">Back to panel</a></section>', 400, request=request)
        raise web.HTTPSeeOther(back)

    async def room_action(self, account, action, room, data):
        email = account["email"]
        row = self.one("SELECT * FROM rooms WHERE id = ?", room)
        access = self.room_access(room)
        if action in {"confirm-vcoin", "return-vcoin-report"} and (
                not account["owner"] or room == self.manager.default_room):
            raise web.HTTPForbidden()
        if action in {"vcoin-order", "vcoin-report", "vcoin-cancel", "confirm-vcoin", "return-vcoin-report",
                      "grant-unlimited", "revoke-unlimited"}:
            if action == "vcoin-order":
                if not (self.billing_test or self.vcoin_recipient):
                    raise ValueError("VCOIN checkout is not available yet.")
                plan = VCOIN_PLANS.get(data.get("plan", "monthly"))
                if not plan:
                    raise ValueError("Choose a monthly or yearly subscription.")
                term = self.one("SELECT expires FROM owner_room_terms WHERE room = ?", room)
                if (access and access["unlimited"]) or (term and term["expires"] is None):
                    raise ValueError("This room already has no-expiration access.")
                amount, duration = plan
                self.db.execute("BEGIN IMMEDIATE")
                with self.db:
                    self.billing_room(room, account)
                    self.db.execute("""INSERT OR IGNORE INTO vcoin_orders(id, room, amount, duration)
                                    VALUES (?, ?, ?, ?)""", (secrets.token_hex(8), room, amount, duration))
            elif action in {"vcoin-report", "vcoin-cancel"}:
                order = self.one("""SELECT * FROM vcoin_orders WHERE id = ? AND room = ?
                                   AND transfer IS NULL""", data.get("order", ""), room)
                if not order:
                    raise ValueError("No matching open VCOIN order.")
                if (order["payer"] or row["customer"]) != email:
                    raise web.HTTPForbidden()
                if action == "vcoin-cancel":
                    if order["reported_sender"]:
                        raise ValueError("A reported transfer cannot be canceled.")
                    self.db.execute("DELETE FROM vcoin_orders WHERE id = ?", (order["id"],))
                    self.db.execute("""DELETE FROM rooms WHERE id = ? AND status = 'pending'
                        AND NOT EXISTS (SELECT 1 FROM vcoin_orders WHERE room = rooms.id)
                        AND NOT EXISTS (SELECT 1 FROM room_access WHERE room = rooms.id)""", (room,))
                else:
                    if row["status"] == "suspended" or order["reported_sender"]:
                        raise ValueError("This transfer report cannot be changed.")
                    sender = data.get("sender", "").strip()
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", sender):
                        raise ValueError("Enter the IMVU username that sent the VCOIN.")
                    event = secrets.token_hex(16)
                    self.db.execute("BEGIN IMMEDIATE")
                    with self.db:
                        updated = self.db.execute("""UPDATE vcoin_orders SET reported_sender = ?, report_event = ?
                            WHERE id = ? AND transfer IS NULL AND reported_sender IS NULL
                              AND EXISTS (SELECT 1 FROM rooms WHERE id = ? AND status <> 'suspended')""",
                                                  (sender, event, order["id"], room))
                        if not updated.rowcount:
                            raise ValueError("This transfer report cannot be changed.")
                        if not self.billing_test:
                            self.db.execute("""INSERT INTO billing_email_outbox(kind, room, event, expires, due)
                                VALUES ('report', ?, ?, 0, ?)""", (room, event, int(time.time())))
            elif action == "grant-unlimited":
                customer = self.one("SELECT email FROM accounts WHERE email = ? AND owner = 0",
                                    data.get("email", "").strip().lower())
                if not customer:
                    raise ValueError("Use a registered customer account.")
                can_join = bool(row and self.room_can_join(room))
                self.db.execute("BEGIN IMMEDIATE")
                with self.db:
                    self.billing_room(room, customer)
                    self.db.execute("""INSERT INTO room_access(room, unlimited, join_pending) VALUES (?, 1, ?)
                        ON CONFLICT(room) DO UPDATE SET unlimited = 1, join_pending = MAX(join_pending, excluded.join_pending)""",
                                    (room, not can_join))
                    self.db.execute("DELETE FROM owner_room_terms WHERE room = ?", (room,))
                    self.db.execute("UPDATE rooms SET status = 'approved' WHERE id = ?", (room,))
            elif action == "revoke-unlimited":
                if access is None or not access["unlimited"]:
                    raise ValueError("This room has no no-expiration grant.")
                self.db.execute("UPDATE room_access SET unlimited = 0 WHERE room = ?", (room,))
            elif action == "return-vcoin-report":
                order_id = data.get("order", "")
                self.db.execute("BEGIN IMMEDIATE")
                with self.db:
                    updated = self.db.execute("""UPDATE vcoin_orders SET reported_sender = NULL
                        WHERE id = ? AND room = ? AND transfer IS NULL AND reported_sender IS NOT NULL""",
                                              (order_id, room))
                    if not updated.rowcount:
                        raise ValueError("No matching open VCOIN order.")
                    self.cancel_report_alert(room, order_id)
            else:
                order = self.one("SELECT * FROM vcoin_orders WHERE id = ? AND room = ?", data.get("order", ""), room)
                if not order:
                    raise ValueError("No matching open VCOIN order.")
                if not self.billing_test and (not order["reported_sender"] or data.get("verified") != "1"):
                    raise ValueError("Match the payment in your IMVU Wallet before confirming it.")
                self.confirm_transfer(order, data.get("transfer", "").strip().lower(), int(time.time()))
        elif action == "request-room":
            self.start_trial(room, account)
        elif not row:
            raise web.HTTPNotFound()
        elif action == "remove-room":
            if not self.one("SELECT 1 FROM accounts WHERE email = ? AND owner = 1", email):
                raise web.HTTPForbidden()
            if data.get("confirm_room", "").strip() != room:
                raise ValueError("Type the exact room ID to confirm removal.")
            await self.remove_room(room, email)
            return
        elif action in {"set-expiration", "no-expiration"}:
            if row["status"] != "approved":
                raise web.HTTPForbidden()
            await self.set_owner_term(room, data.get("end_date", "") if action == "set-expiration" else None)
        elif action == "decline":
            if row["status"] != "pending" or self.one("SELECT 1 FROM vcoin_orders WHERE room = ?", room):
                raise web.HTTPForbidden()
            self.db.execute("DELETE FROM rooms WHERE id = ?", (room,))
        elif action in {"approve", "suspend"}:
            if room == self.manager.default_room:
                raise web.HTTPForbidden()
            status = "approved" if action == "approve" else "suspended"
            with_transaction = action == "suspend"
            self.db.execute("BEGIN")
            try:
                self.db.execute("UPDATE rooms SET status = ? WHERE id = ?", (status, room))
                if with_transaction:
                    self.db.execute("DELETE FROM grants WHERE room = ?", (room,))
                    self.db.execute("""DELETE FROM billing_email_outbox
                        WHERE kind = 'report' AND room = ? AND delivered IS NULL""", (room,))
                self.db.execute("COMMIT")
            except sqlite3.Error:
                self.db.execute("ROLLBACK")
                raise
        elif row["status"] != "approved":
            raise web.HTTPForbidden()
        elif action in {"grant", "revoke"}:
            target = data.get("email", "").strip().lower()
            if action == "grant":
                if not self.one("SELECT email FROM accounts WHERE email = ? AND owner = 0", target) or target == row["customer"]:
                    raise ValueError("Enter the email of a registered account other than the room customer.")
                self.db.execute("INSERT OR IGNORE INTO grants VALUES (?, ?)", (room, target))
            else:
                self.db.execute("DELETE FROM grants WHERE room = ? AND email = ?", (room, target))
            action += " " + target
        elif action in {"join", "leave"}:
            if action == "join" and not self.room_can_join(room):
                raise web.HTTPForbidden()
            await getattr(self.manager, action)(room)
            if action == "leave":
                self.pause_room(room)
        elif action in {"settings", "language"}:
            if not self.room_can_join(room):
                raise web.HTTPForbidden()
            if not (self.site_admin(account) or row["customer"] == email or
                    self.one("SELECT 1 FROM grants WHERE room = ? AND email = ?", room, email)):
                raise web.HTTPForbidden()
            if action == "language":
                await self.manager.set_room_language(room, data.get("language", ""))
            else:
                await self.manager.settings.update(room, {
                    "quiet": "quiet" in data, "welcomes": "welcomes" in data,
                    "welcome": data.get("welcome", ""),
                })
        self.db.execute("INSERT INTO audit(actor, room, action) VALUES (?, ?, ?)", (email, room, action))


def install_website(app, manager):
    portal = Portal(manager)
    manager.room_access_check = portal.room_can_join
    manager.room_paused = portal.pause_room
    manager.room_health_changed = portal.record_room_health
    if not portal.billing_test:
        manager.room_registered = portal.register_owner_room
    app.middlewares.append(website_locale)
    app.router.add_get("/locale", set_website_locale)
    app.router.add_get("/locale/{language}", set_website_locale)
    app.router.add_get("/", portal.home)
    app.router.add_get("/guide", portal.guide)
    async def css(_):
        return web.FileResponse(Path(__file__).with_name("ramsei_web.css"))

    async def share_card(_):
        image = Path(__file__).with_name("ramzy_share.png.b64").read_bytes()
        return web.Response(body=base64.b64decode(image), content_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    async def favicon(_):
        return web.Response(content_type="image/svg+xml", text="""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<defs><linearGradient id="red" x2="1" y2="1"><stop stop-color="#ff465d"/><stop offset="1" stop-color="#a50c2b"/></linearGradient></defs>
<path fill="url(#red)" d="M0 0h26l6 6v26H6l-6-6Z"/>
<path fill="#fff" d="m10 8-3 16h5l1-5h2l2 5h5l-3-6c3-1 4-3 4-5 0-3-2-5-6-5Zm4 4h3c1 0 2 .5 2 1.5S18 15 17 15h-3Z"/>
</svg>""")

    async def language_script(_):
        return web.Response(content_type="application/javascript", text="""
const picker = document.querySelector('.site-languages select');
picker?.addEventListener('change', () => picker.form.requestSubmit());

const pointer = document.querySelector('.pointer-fx');
const finePointer = matchMedia('(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)');
let frame = 0;
let point;
addEventListener('pointermove', event => {
    if (!finePointer.matches || event.pointerType !== 'mouse') return;
    point = event;
    if (!frame) frame = requestAnimationFrame(() => {
        pointer.style.transform = 'translate3d(' + point.clientX + 'px,' + point.clientY + 'px,0)';
        pointer.classList.add('visible');
        pointer.classList.toggle('over-control', Boolean(point.target.closest?.('a, button, select, input')));
        frame = 0;
    });
}, {passive: true});
document.documentElement.addEventListener('pointerleave', () => pointer.classList.remove('visible'));
addEventListener('pointerdown', event => {
    if (finePointer.matches && event.pointerType === 'mouse') {
        pointer.firstElementChild.animate([
            {scale: '0.75', opacity: 1}, {scale: '1.8', opacity: 0}
        ], {duration: 350, easing: 'ease-out'});
    }
}, {passive: true});
""")

    async def health(_):
        return web.json_response({"status": "ok"})

    app.router.add_get("/site.css", css)
    app.router.add_get("/favicon.svg", favicon)
    app.router.add_get("/share-card.png", share_card)
    app.router.add_get("/site-language.js", language_script)
    app.router.add_get("/health", health)
    for route in ("/signin", "/signup"):
        app.router.add_get(route, portal.auth)
        app.router.add_post(route, portal.auth)
    for route, handler in (("/forgot-password", portal.forgot_password), ("/reset-password", portal.reset_password)):
        app.router.add_get(route, handler)
        app.router.add_post(route, handler)
    app.router.add_get("/verify-email", portal.verify_email)
    app.router.add_post("/verify-email", portal.verify_email)
    app.router.add_get("/account", portal.account_settings)
    app.router.add_post("/account", portal.account_settings)
    for route in ("/panel", "/owner"):
        app.router.add_get(route, portal.panel)
    app.router.add_get("/owner/payments", portal.payment_inbox)
    app.router.add_get("/payments", portal.payment_history)
    for route in ("/request-room", "/signout", "/settings", "/language", "/owner/approve", "/owner/decline", "/owner/suspend", "/owner/remove-room",
                  "/owner/grant", "/owner/revoke", "/owner/join", "/owner/leave", "/vcoin-order",
                  "/vcoin-report", "/vcoin-cancel",
                  "/owner/confirm-vcoin", "/owner/return-vcoin-report", "/owner/grant-unlimited", "/owner/revoke-unlimited",
                  "/owner/set-expiration", "/owner/no-expiration", "/owner/add-site-admin", "/owner/remove-site-admin"):
        app.router.add_post(route, portal.action)

    async def billing_email_context(_):
        task = asyncio.create_task(portal.watch_billing_email())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    if not portal.billing_test:
        app.cleanup_ctx.append(billing_email_context)

    async def close(_):
        await portal.close_password_reset_mail()
        portal.db.close()

    app.on_cleanup.append(close)
    return portal
