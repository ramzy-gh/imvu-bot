"""Room-local boot command with live native moderator and bot-role protection."""
import asyncio
import logging
import re
from urllib.parse import urlparse

from identity import resolve_username, UserLookupError
from permissions import OWNER_ID

API = "https://api.imvu.com"
LOG = logging.getLogger("ramsei.moderation")


async def native_moderators(bot):
    base = f"/room/room-{bot.room}/moderators"
    path, found = base, set()
    visited = set()
    for _ in range(20):
        if path in visited:
            raise ValueError("Moderator list pagination repeated; nothing was booted.")
        visited.add(path)
        data = await bot.api("GET", path)
        node = data.get("denormalized", {}).get(API + path)
        if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
            raise ValueError("Could not verify IMVU room moderators; nothing was booted.")
        info = node["data"]
        items = info.get("items")
        if not isinstance(items, list):
            raise ValueError("Could not verify IMVU room moderators; nothing was booted.")
        for item in items:
            url = item.get("id") if isinstance(item, dict) else item
            match = re.fullmatch(r"https://api\.imvu\.com/(?:user/|room/room-" + re.escape(bot.room) +
                                 r"/moderators/)user-([1-9]\d*)", str(url))
            if not match:
                raise ValueError("Unsupported moderator record; nothing was booted.")
            found.add(match[1])
        nxt = node.get("relations", {}).get("next") or info.get("next")
        if not nxt:
            total = info.get("total_count", len(found))
            if isinstance(total, int) and total > len(found):
                raise ValueError("Incomplete moderator list; nothing was booted.")
            return found
        if isinstance(nxt, dict):
            nxt = nxt.get("id", "")
        parsed = urlparse(str(nxt))
        if parsed.scheme != "https" or parsed.netloc != "api.imvu.com" or parsed.path != base:
            raise ValueError("Unsupported moderator list pagination; nothing was booted.")
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
    raise ValueError("Moderator list is too large to verify safely; nothing was booted.")


def authorized(bot, actor, moderators):
    return (actor in bot.members and bot.allowed_in_room(actor) and
            (bot.permissions.is_admin(actor, bot.room) or bot.permissions.is_mod(actor)
             or actor in moderators or actor == bot.room.split("-")[0]))


def protected(bot, target, moderators):
    return (target in {OWNER_ID, bot.user_id, bot.room.split("-")[0]} or target in moderators
            or bot.permissions.is_mod(target)
            or any(target in admins for admins, _ in bot.permissions.room_moderation.values()))


async def boot_user(bot, actor, verified, query):
    actor = str(actor)
    if not verified or actor not in bot.members or not bot.allowed_in_room(actor):
        await bot.say("Only a verified admin present in this room can use !boot.")
        return
    if not query or len(query.split()) != 1:
        await bot.say("Usage: !boot IMVUUsername (exact account username, not nickname).")
        return
    try:
        async with asyncio.timeout(30):
            moderators = await native_moderators(bot)
            if not authorized(bot, actor, moderators):
                await bot.say("Only this room's admins or moderators can use !boot.")
                return
            target, username = await resolve_username(bot.api, query)
            await bot.participants()
            # Re-read roles after lookup/membership I/O, so promotions and revocations apply immediately.
            moderators = await native_moderators(bot)
            if not authorized(bot, actor, moderators):
                await bot.say("Your room-admin access changed. Nothing was booted.")
                return
            if protected(bot, target, moderators):
                await bot.say("That user is protected: owners, room moderators and bot admins cannot be booted.")
                return
            if target not in bot.members:
                await bot.say("@" + username + " is not present in this room.")
                return
            if bot.user_id not in moderators and bot.user_id != bot.room.split("-")[0]:
                await bot.say("The bot must be an IMVU room moderator to boot users. The room owner needs to grant that role.")
                return
            if not bot.connected or not bot.chat_ready:
                await bot.say("The room is reconnecting. Nothing was booted.")
                return
            # Normal IMVU boot announcement plus authoritative participant removal.
            if not await bot.say("*imvu:txnBoot " + target):
                return
            if not await bot.say("*boot " + bot.user_id + " " + target):
                return
            try:
                await bot.api("DELETE", f"/chat/chat-{bot.room}/participants/user-{target}")
            except RuntimeError as error:
                if getattr(error, "status", None) != 404:
                    raise
            LOG.info("Room boot accepted: room=%s actor=%s target=%s", bot.room, actor, target)
            portal = getattr(bot.room_manager, "portal", None)
            if portal:
                portal.db.execute("INSERT INTO audit(actor,room,action) VALUES (?, ?, ?)",
                                  ("imvu:" + actor, bot.room, "boot:" + target))
            await bot.say("Booted @" + username + " from this room.")
    except UserLookupError as error:
        await bot.say(str(error))
    except ValueError as error:
        await bot.say(str(error))
    except Exception as error:
        LOG.warning("Boot request could not finish: room=%s type=%s", bot.room, type(error).__name__)
        await bot.say("IMVU could not complete the boot. Check the bot's native room-moderator permission and try again.")
