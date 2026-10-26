"""Move only this bot to a real, unoccupied seat near a room member."""
import asyncio
import math
import re
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlparse

import aiohttp

API = "https://api.imvu.com"
ASSETS = "https://asset-server-akm.imvu.com"


def asset_url(value):
    url = urljoin(ASSETS, value)
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"asset-server-akm.imvu.com", "asset-server.imvu.com"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.startswith("/asset/")):
        raise ValueError("Unsupported IMVU room asset.")
    return url


def vector(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError("Invalid room transform.")
    result = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in result):
        raise ValueError("Invalid room transform.")
    return result


def quaternion(value):
    q = vector(value, 4)
    length = math.sqrt(sum(v*v for v in q))
    if length < 1e-10:
        raise ValueError("Invalid room rotation.")
    return tuple(v / length for v in q)


def multiply(a, b):
    x, y, z, w = a
    X, Y, Z, W = b
    return (w*X+x*W+y*Z-z*Y, w*Y-x*Z+y*W+z*X,
            w*Z+x*Y-y*X+z*W, w*W-x*X-y*Y-z*Z)


def rotate(q, v):
    return multiply(multiply(q, (*v, 0)), (-q[0], -q[1], -q[2], q[3]))[:3]


def skeleton_seats(data):
    """Compose bone transforms; seat numbers themselves are not distances."""
    bones = data.get("bones", [])
    if not isinstance(bones, list) or len(bones) > 20000:
        raise ValueError("Unsupported room skeleton.")
    resolved, active = {}, set()
    def world(index):
        if index in resolved:
            return resolved[index]
        if index in active or len(active) >= 100 or not 0 <= index < len(bones):
            raise ValueError("Invalid room skeleton hierarchy.")
        active.add(index)
        bone = bones[index]
        transform = bone["relativeTransform"]
        p, q = vector(transform["translation"], 3), quaternion(transform["rotation"])
        parent = int(bone.get("parentIndex", -1))
        if parent >= 0:
            pp, pq = world(parent)
            p = tuple(a+b for a, b in zip(pp, rotate(pq, p)))
            q = quaternion(multiply(pq, q))
        active.remove(index)
        resolved[index] = p, q
        return p, q
    seats = {}
    for index, bone in enumerate(bones):
        match = re.fullmatch(r"seat(\d+)\.[^.]+", str(bone.get("name", "")), re.I)
        if match:
            number = int(match[1])
            if number in seats:
                raise ValueError("Room has ambiguous seat numbers.")
            seats[number] = world(index)[0]
    return seats


def participant_seats(data, room):
    found = {}
    prefix = API + f"/chat/chat-{room}/participants/user-"
    for url, node in data.get("denormalized", {}).items():
        if not url.startswith(prefix) or not url[len(prefix):].isdigit():
            continue
        info = node.get("data", {})
        try:
            fid, seat = int(info["seat_furni_id"]), int(info["seat_number"])
            if fid >= 0 and seat >= 0:
                found[url[len(prefix):]] = (str(fid), seat)
        except (KeyError, ValueError, TypeError):
            continue
    return found


def legacy_seat_message(data, room, uid):
    """Use IMVU's own Classic message, strictly bound to this avatar and seat."""
    address = participant_seats(data, room).get(uid)
    node = data.get("denormalized", {}).get(API + f"/chat/chat-{room}/participants/user-{uid}", {})
    message = node.get("data", {}).get("legacy_seat_message")
    if address is None or not isinstance(message, str):
        raise ValueError("IMVU did not provide a Classic seat announcement.")
    fid, seat = address
    if not re.fullmatch(r"\*msg SeatAssignment \d+ " + re.escape(uid) + " " + str(seat) + " " + fid, message):
        raise ValueError("IMVU returned an inconsistent Classic seat announcement.")
    return message


class Movement:
    def __init__(self, bot):
        self.bot = bot
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.last_move = -1000

    async def fetch_asset(self, url):
        url = asset_url(url)
        cached = self.cache.get(url)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        # Asset downloads never receive the bot's authenticated API headers.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError("Could not load this room's seating layout.")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise ValueError("Room seating layout is too large.")
                    chunks.append(chunk)
        import json
        result = json.loads(b"".join(chunks))
        self.cache[url] = time.monotonic(), result
        self.cache.move_to_end(url)
        while len(self.cache) > 32:
            self.cache.popitem(last=False)
        return result

    async def seat_map(self, fid):
        path = f"/scene/scene-{self.bot.room}"
        scene = await self.bot.api("GET", path)
        if fid == "0":
            url = scene.get("denormalized", {}).get(API + path, {}).get("data", {}).get("room_asset_url")
        else:
            url = None
            collection = path + "/furniture"
            for _ in range(10):
                page = await self.bot.api("GET", collection)
                nodes = page.get("denormalized", {})
                for node in nodes.values():
                    info = node.get("data", {})
                    if str(info.get("instance_id")) == fid:
                        url = info.get("asset_url")
                        break
                if url:
                    break
                next_url = nodes.get(API + collection, {}).get("relations", {}).get("next")
                if not isinstance(next_url, str) or not next_url.startswith(API + path + "/furniture?"):
                    break
                collection = next_url[len(API):]
        if not isinstance(url, str):
            raise ValueError("Could not find seating data for your current spot.")
        manifest = await self.fetch_asset(url)
        result = {}
        for obj in manifest.get("objects", [])[:16]:
            skeleton = obj.get("skeleton", {}).get("url")
            if skeleton:
                for seat, position in skeleton_seats(await self.fetch_asset(skeleton)).items():
                    if seat in result:
                        raise ValueError("This furniture has ambiguous seat coordinates.")
                    result[seat] = position
        return result

    async def move_near(self, uid):
        bot = self.bot
        async with self.lock:
            if not bot.connected or not bot.chat_ready or uid not in bot.members or not bot.permissions.allowed(uid):
                return None
            if time.monotonic() - self.last_move < 10:
                return "Give me a moment before moving again."
            path = f"/chat/chat-{bot.room}/participants"
            seats = participant_seats(await bot.api("GET", path), bot.room)
            target = seats.get(uid)
            if target is None:
                return "I can't locate your avatar's seat. Try !move again after your room loads."
            fid, number = target
            positions = await self.seat_map(fid)
            if number not in positions:
                return "I couldn't locate a valid spot near you in this room layout."
            # Recheck after loading assets: people can move or leave meanwhile.
            seats = participant_seats(await bot.api("GET", path), bot.room)
            if uid not in bot.members or not bot.permissions.allowed(uid) or seats.get(uid) != target:
                return "You moved or left while I checked the seats. Try !move again."
            occupied = set(seats.values())
            candidates = sorted((seat for seat in positions if (fid, seat) not in occupied),
                                key=lambda seat: (math.dist(positions[number], positions[seat]), seat))
            if not candidates:
                return "There isn't a free spot near you on that room or furniture item."
            chosen = candidates[0]
            own_path = path + f"/user-{bot.user_id}"
            await bot.api("POST", own_path, json={"seat_furni_id": int(fid), "seat_number": chosen})
            confirmation = await bot.api("GET", own_path)
            actual = participant_seats(confirmation, bot.room).get(bot.user_id)
            if actual != (fid, chosen):
                return "IMVU didn't confirm my new spot. Please try !move again."
            # The official client broadcasts this after a REST seat change.
            # Classic clients do not reliably render a REST-only update.
            if not await bot.say(legacy_seat_message(confirmation, bot.room, bot.user_id)):
                return "My seat changed, but the room announcement failed. Please try !move again."
            self.last_move = bot.last_seat_announcement = time.monotonic()
            return "Moved to a free spot near you!"
