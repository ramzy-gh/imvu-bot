"""Opt-in room invitations requested in one-to-one messages sent to this account."""
import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone

LOG = logging.getLogger("ramsei.invites")
API = "https://api.imvu.com"
CONVERSATION = re.compile(r"https://api\.imvu\.com/conversation/conversation-\d+")
USER = re.compile(r"https://api\.imvu\.com/user/user-([1-9]\d*)")


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def invite_query(message):
    payloads = message.get("payloads", [])
    if not isinstance(payloads, list):
        return None
    texts = [p.get("content") for p in payloads
             if isinstance(p, dict) and p.get("type") == "text"]
    if len(texts) != 1 or not isinstance(texts[0], str) or len(texts[0]) > 600:
        return None
    match = re.fullmatch(r"\s*!invite(?:\s+([^\r\n]*))?\s*", texts[0], re.IGNORECASE)
    return match[1].strip() if match and match[1] else "" if match else None


def sender_id(conversation, message, own_id):
    """Use authenticated API sender metadata, never names or IDs in message text."""
    participants = conversation.get("participants", [])
    if not isinstance(participants, list) or len(participants) != 2:
        return None
    users = []
    for participant in participants:
        url = participant.get("user", "") if isinstance(participant, dict) else ""
        match = USER.fullmatch(url) if isinstance(url, str) else None
        if not match:
            return None
        users.append(match[1])
    sent_by = message.get("sent_by", "")
    match = USER.fullmatch(sent_by) if isinstance(sent_by, str) else None
    if not match or str(own_id) not in users or len(set(users)) != 2:
        return None
    uid = match[1]
    # Both sender representations in IMVU's message must agree.
    if message.get("sender") != API + "/users/" + uid:
        return None
    return uid if uid != str(own_id) and uid in users else None


def select_room(manager, query):
    rooms = [bot for bot, task in manager.rooms.values()
             if not task.done() and bot.connected and bot.chat_ready]
    query = query.strip()
    link = re.fullmatch(r"https://(?:www\.)?imvu\.com/next/chat/room-([1-9]\d*-\d+)/?", query)
    if link:
        query = link[1]
    matches = [bot for bot in rooms if bot.room == query]
    if not matches:
        matches = [bot for bot in rooms if bot.room_name.casefold() == query.casefold()]
    if len(matches) > 1:
        raise ValueError("Several rooms have that name. Use a room ID: " + ", ".join(b.room for b in matches))
    if not matches:
        raise ValueError("Use !invite <room ID or exact room name>. Ramsei must be connected to that room.")
    return matches[0]


class PrivateInvites:
    def __init__(self, manager):
        self.manager = manager
        self.started = datetime.now(timezone.utc)
        self.seen = OrderedDict()
        self.cooldowns = OrderedDict()
        self.versions = OrderedDict()
        self.global_requests = deque()
        self.ready_logged = False

    async def reply(self, api_bot, conversation_url, text):
        if not CONVERSATION.fullmatch(conversation_url):
            return
        await api_bot.api("POST", conversation_url[len(API):] + "/messages",
                          json={"payloads": [{"type": "text", "content": text[:450]}]})

    def permit(self, uid):
        now = time.monotonic()
        while self.global_requests and self.global_requests[0] <= now - 60:
            self.global_requests.popleft()
        if now - self.cooldowns.get(uid, -1000) < 30 or len(self.global_requests) >= 20:
            return False
        self.cooldowns[uid] = now
        self.cooldowns.move_to_end(uid)
        while len(self.cooldowns) > 2000:
            self.cooldowns.popitem(last=False)
        self.global_requests.append(now)
        return True

    async def handle(self, api_bot, conversation_url, conversation, message):
        uid = sender_id(conversation, message, api_bot.user_id)
        query = invite_query(message)
        created = timestamp(message.get("created"))
        message_id = message.get("message_id")
        if (not uid or query is None or not isinstance(message_id, str)
                or not re.fullmatch(r"message-\d+-\d+", message_id)
                or not created or created < self.started
                or not self.manager.permissions.globally_allowed(uid)):
            return
        if message_id in self.seen:
            return
        # Record before any network operation: retries never resend an invite.
        self.seen[message_id] = None
        while len(self.seen) > 2000:
            self.seen.popitem(last=False)
        if not self.permit(uid):
            return
        try:
            room = select_room(self.manager, query)
        except ValueError as error:
            await self.reply(api_bot, conversation_url, str(error))
            return
        if not self.manager.permissions.allowed(uid, room.room):
            return
        try:
            # Normal IMVU invite API. No occupancy check: IMVU decides admission.
            await room.api("POST", f"/chat/chat-{room.room}/invites",
                           json={"relations": {"recipient": API + "/user/user-" + uid}})
        except Exception as error:
            LOG.warning("Room invitation rejected (%s); no message content logged", type(error).__name__)
            await self.reply(api_bot, conversation_url,
                             "IMVU could not send that invitation. Check the room's invite permissions and try again later.")
            return
        LOG.info("Requested room invitation accepted by IMVU; room=%s", room.room)
        await self.reply(api_bot, conversation_url,
                         "Invitation sent for " + room.room_name + ". Accept it in IMVU. Room access and capacity rules still apply.")

    async def poll(self, api_bot):
        response = await api_bot.api("GET", f"/user/user-{api_bot.user_id}/conversations?limit=50")
        if not self.ready_logged:
            LOG.info("Private !invite listener ready; only new one-to-one requests to Ramsei are processed")
            self.ready_logged = True
        for url, node in response.get("denormalized", {}).items():
            if not CONVERSATION.fullmatch(url):
                continue
            conversation = node.get("data", {})
            last = conversation.get("last_message", {})
            if not isinstance(last, dict):
                continue
            created = timestamp(last.get("created"))
            if not created or created < self.started:
                continue
            version = last.get("message_id")
            if not isinstance(version, str) or self.versions.get(url) == version:
                continue
            # Read recent messages to avoid losing a command when another DM follows it.
            history = await api_bot.api("GET", url[len(API):] + "/messages?limit=20")
            messages = [v.get("data", {}) for k, v in history.get("denormalized", {}).items()
                        if re.fullmatch(re.escape(API) + r"/message/message-\d+-\d+", k)]
            messages.sort(key=lambda m: m.get("created", ""))
            for message in messages:
                await self.handle(api_bot, url, conversation, message)
            self.versions[url] = version
            self.versions.move_to_end(url)
            while len(self.versions) > 2000:
                self.versions.popitem(last=False)

    async def run(self):
        while True:
            delay = 10
            bots = [bot for bot, task in self.manager.rooms.values()
                    if not task.done() and bot.connected and bot.chat_ready]
            if bots:
                try:
                    await self.poll(bots[0])
                except Exception as error:
                    # DM failure must never disconnect room chat or trigger extra logins.
                    LOG.warning("Private invite check deferred (%s); room chat continues", type(error).__name__)
                    delay = 60
            await asyncio.sleep(delay)
