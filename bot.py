"""Experimental, independently implemented IMVU room bot; live validation required."""
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL
from personality import ALIASES, Social
from permissions import Permissions, MANAGEMENT_COMMANDS
from identity import resolve_username, UserLookupError
from state_store import StateStore

LOG = logging.getLogger("ramsei")
API = "https://api.imvu.com"
IMQ = "wss://wss-imq.imvu.com/streaming/imvu_pre"


class AuthenticationRequired(RuntimeError):
    pass


def encode(value):
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_message(packet, queue, members, own_id):
    """Accept only public messages in the joined queue from present participants."""
    if packet.get("record") != "msg_g2c_send_message" or packet.get("queue") != queue:
        return None
    if packet.get("mount") != "messages":
        return None
    try:
        payload = json.loads(base64.b64decode(packet["message"], validate=True))
        if not isinstance(payload, dict):
            return None
        sender = str(payload["userId"])
        text = payload["message"]
        # Bind sender to server identity when IMQ supplies it.
        verified = packet.get("user_id", packet.get("sender_id"))
        if verified is not None and str(verified) != sender:
            return None
        if sender == own_id or sender not in members or payload.get("to", 0) not in (0, "0", None):
            return None
        if not isinstance(text, str) or text.startswith("*") or len(text) > 600:
            return None
        return sender, text, verified is not None
    except (ValueError, KeyError, TypeError, UnicodeError):
        return None


async def capture(*args, timeout=60):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode:
            raise RuntimeError("YouTube lookup failed; check server access and yt-dlp version.")
        return out
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class Radio:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=30)
        self.listeners = set()
        self.current = None
        self.proc = None
        self.idle = True
        self.skip = asyncio.Event()
        self.accepting = False

    async def lookup(self, query):
        # Only YouTube search terms / YouTube URLs; no arbitrary remote URL input.
        if len(query) > 250 or "\n" in query:
            raise ValueError("Use a shorter song name.")
        if re.search(r"\w+://", query):
            from urllib.parse import urlparse
            parsed = urlparse(query)
            if parsed.scheme != "https" or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}:
                raise ValueError("Use a song name or an HTTPS YouTube link.")
            target = query
        else:
            target = "ytsearch1:" + query
        data = json.loads(await capture(sys.executable, "-m", "yt_dlp", "--no-warnings",
            "--no-playlist", "--skip-download", "--dump-single-json", "--js-runtimes", "node",
            "--socket-timeout", "15", "--retries", "1", "-f", "bestaudio/best", "--", target))
        if "entries" in data:
            data = next((item for item in data["entries"] if item), {})
        if not data.get("url") or not data.get("duration") or data.get("is_live"):
            raise ValueError("No playable song found. Live streams are not supported.")
        if data["duration"] > 900:
            raise ValueError("Please choose a track under 15 minutes.")
        # The extractor supplies media URLs. FFmpeg may only open HTTPS/TLS sources.
        if not data["url"].startswith("https://"):
            raise ValueError("This result has no supported secure audio source.")
        return {"title": str(data.get("title", "Unknown track"))[:140],
                "url": data["url"], "duration": data["duration"]}

    def interrupt(self):
        self.skip.set()

    async def broadcast(self, chunk):
        for listener in tuple(self.listeners):
            if listener.full():
                # Slow listeners reconnect instead of building an unlimited delay.
                self.listeners.discard(listener)
                while not listener.empty():
                    listener.get_nowait()
                listener.put_nowait(None)
            else:
                listener.put_nowait(chunk)

    async def stream(self, request):
        if len(self.listeners) >= 50:
            raise web.HTTPServiceUnavailable(text="Listener limit reached")
        response = web.StreamResponse(headers={"Content-Type": "audio/mpeg",
            "Cache-Control": "no-store", "X-Accel-Buffering": "no", "icy-name": "RAMSEI Radio"})
        await response.prepare(request)
        listener = asyncio.Queue(maxsize=64)
        self.listeners.add(listener)
        try:
            while True:
                chunk = await listener.get()
                if chunk is None:
                    break
                await asyncio.wait_for(response.write(chunk), 15)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            self.listeners.discard(listener)
        return response

    async def run(self, announce):
        while True:
            track = None
            try:
                track = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.current = track
            self.idle = track is None
            self.skip.clear()
            args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-re"]
            if track:
                args += ["-protocol_whitelist", "https,tls,tcp,crypto", "-rw_timeout", "15000000",
                         "-i", track["url"], "-t", "900"]
            else:
                args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
            args += ["-vn", "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame",
                     "-b:a", "128k", "-write_xing", "0", "-f", "mp3", "pipe:1"]
            read_task = stop_task = None
            try:
                self.proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                if track:
                    await announce("Now playing: " + track["title"])
                async def pump():
                    while chunk := await self.proc.stdout.read(4096):
                        await self.broadcast(chunk)
                read_task = asyncio.create_task(pump())
                stop_task = asyncio.create_task(self.skip.wait())
                await asyncio.wait({read_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (read_task, stop_task):
                    if task:
                        task.cancel()
                await asyncio.gather(*(t for t in (read_task, stop_task) if t), return_exceptions=True)
                if self.proc and self.proc.returncode is None:
                    self.proc.terminate()
                    try:
                        await asyncio.wait_for(self.proc.wait(), 5)
                    except asyncio.TimeoutError:
                        self.proc.kill()
                        await self.proc.wait()
                if self.proc and self.proc.returncode and not self.skip.is_set():
                    LOG.warning("Audio encoder stopped (code %s)", self.proc.returncode)
                    await asyncio.sleep(2)
                self.proc = None
                if track:
                    self.queue.task_done()


class Bot:
    def __init__(self, radio):
        self.radio = radio
        self.room = os.getenv("IMVU_ROOM_ID", "330286535-177")
        if not re.fullmatch(r"\d+-\d+", self.room):
            raise ValueError("IMVU_ROOM_ID must look like 330286535-177")
        self.permissions = Permissions()
        self.members = set()
        self.greeted = OrderedDict()
        self.cooldowns = {}
        self.commands = asyncio.Queue(maxsize=50)
        self.ws = None
        self.connected = False
        self.session = None
        self.op = 0
        self.last_said = 0
        self.say_lock = asyncio.Lock()
        self.social = Social(self.say, allowed=self.permissions.allowed)

    async def api(self, method, path, **kwargs):
        async with self.session.request(method, API + path, **kwargs) as response:
            if response.status == 401:
                raise AuthenticationRequired("IMVU session expired; a fresh login may require 2FA.")
            if response.status not in (200, 201, 204):
                raise RuntimeError(f"IMVU request failed: HTTP {response.status} at {path.split('?')[0]}")
            return await response.json() if response.status != 204 else {}

    def session_path(self):
        return Path(os.getenv("SESSION_PATH", "/app/data/imvu-session.json"))

    def restore_session(self):
        try:
            saved = StateStore("session:" + os.getenv("IMVU_USERNAME", "Ramsei").lower(), self.session_path()).load()
            if saved is None:
                return False
            if saved.get("username", "").lower() != os.getenv("IMVU_USERNAME", "Ramsei").lower():
                return False
            if not re.fullmatch(r"\d+", str(saved["user_id"])):
                return False
            self.user_id = str(saved["user_id"])
            self.session.cookie_jar.update_cookies(saved["cookies"], response_url=URL(API))
            self.cookie = self.session.cookie_jar.filter_cookies(URL(API)).get("osCsid")
            if not self.cookie:
                return False
            self.session.headers.update({"X-Imvu-Sauce": saved["sauce"], "X-Imvu-Application": "next_desktop/1"})
            LOG.info("Restored saved session; IMVU will validate it when joining the room")
            return True
        except OSError:
            if os.getenv("DATABASE_URL", "").strip():
                raise RuntimeError("External session database unavailable; check DATABASE_URL privately.") from None
            return False
        except (ValueError, KeyError, TypeError):
            return False

    def persist_session(self, sauce):
        saved = {"username": os.getenv("IMVU_USERNAME", "Ramsei"), "user_id": self.user_id,
                 "sauce": sauce, "cookies": {k: v.value for k, v in self.session.cookie_jar.filter_cookies(URL(API)).items()}}
        try:
            StateStore("session:" + saved["username"].lower(), self.session_path()).save(saved)
            LOG.info("Session saved; remove IMVU_2FA_CODE after successful connection")
        except OSError:
            if os.getenv("DATABASE_URL", "").strip():
                raise RuntimeError("Could not save the session to the external database.") from None
            LOG.warning("Session could not be saved; ensure SESSION_PATH points to writable persistent storage")

    async def login(self, force=False):
        if not force and self.restore_session():
            return
        username = os.getenv("IMVU_USERNAME", "Ramsei")
        password = os.getenv("IMVU_PASSWORD", "")
        if not password:
            raise RuntimeError("Set IMVU_PASSWORD privately in Bothost environment variables.")
        payload = {"username": username, "password": password, "gdpr_cookie_acceptance": False}
        if os.getenv("IMVU_2FA_CODE"):
            payload.update({"2fa_code": os.environ["IMVU_2FA_CODE"], "remember_device": True})
        async with self.session.post(API + "/login", json=payload) as response:
            if response.status == 202:
                raise RuntimeError("IMVU requires verification. Enter the current code as IMVU_2FA_CODE and restart.")
            if response.status not in (200, 201):
                raise RuntimeError(f"Login rejected: HTTP {response.status}. Check credentials/verification in IMVU.")
            data = await response.json()
        nodes = data.get("denormalized", {}).values()
        auth = next((node.get("data", {}) for node in nodes if node.get("data", {}).get("sauce")), {})
        user = auth.get("user", {})
        user_url = user.get("id", "") if isinstance(user, dict) else str(user)
        match = re.search(r"(?:cid/|user-)(\d+)", user_url)
        if not match or not auth.get("sauce"):
            raise RuntimeError("Unrecognized IMVU login response; connector needs adjustment.")
        self.user_id = match[1]
        cookies = self.session.cookie_jar.filter_cookies(URL(API))
        self.cookie = cookies.get("osCsid")
        if self.cookie is None:
            raise RuntimeError("IMVU did not issue the expected session cookie.")
        self.session.headers.update({"X-Imvu-Sauce": auth["sauce"], "X-Imvu-Application": "next_desktop/1"})
        await asyncio.to_thread(self.persist_session, auth["sauce"])

    async def packet(self, record, **fields):
        if self.ws is None or self.ws.closed:
            raise ConnectionError("IMVU chat is disconnected")
        self.op += 1
        await self.ws.send_json({"record": record, "op_id": self.op, **fields})

    async def say(self, text):
        if not self.connected:
            return
        async with self.say_lock:
            await asyncio.sleep(max(0, self.last_said + 1.5 - time.monotonic()))
            if not self.connected:
                return
            await self.packet("msg_c2g_send_message", queue=self.queue, mount="messages",
                message=encode({"chatId": self.queue.rsplit("/", 1)[-1], "message": text[:450],
                                "to": 0, "userId": self.user_id}))
            self.last_said = time.monotonic()

    async def participants(self, greet=False):
        path = f"/chat/chat-{self.room}/participants"
        data = await self.api("GET", path)
        new = set()
        for url, node in data.get("denormalized", {}).items():
            match = re.search(r"/participants/user-(\d+)$", url)
            if match:
                new.add(match[1])
        if greet:
            for uid in sorted(new - self.members - {self.user_id}):
                now = time.monotonic()
                if now - self.greeted.get(uid, -1000) < 300:
                    continue
                self.greeted[uid] = now
                self.greeted.move_to_end(uid)
                while len(self.greeted) > 500:
                    self.greeted.popitem(last=False)
                user = await self.api("GET", f"/user/user-{uid}")
                info = user.get("denormalized", {}).get(API + f"/user/user-{uid}", {}).get("data", {})
                name = info.get("display_name") or info.get("username") or "friend"
                template = os.getenv("WELCOME_MESSAGE", "نورت الروم يا {name}! 😎💛 أنا رامسي البوت، للمزيكا !play واسم الأغنية، وللعب !game 🎲")
                await self.say(template.replace("{name}", str(name)[:60]))
        self.members = new

    async def presence(self):
        while True:
            await asyncio.sleep(15)
            await self.packet("msg_c2g_ping")
            await self.api("POST", f"/chat/chat-{self.room}/participants/user-{self.user_id}", json={})
            await self.participants(greet=True)

    async def receive(self):
        async for message in self.ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            try:
                packet = json.loads(message.data)
            except ValueError:
                continue
            if packet.get("record") == "msg_g2c_result" and packet.get("status", 0) != 0:
                raise RuntimeError("IMVU rejected a chat operation; connector needs live diagnosis.")
            command = decode_message(packet, self.queue, self.members, self.user_id)
            if command:
                uid, text, verified = command
                if not self.permissions.allowed(uid):
                    continue
                if text.startswith("!") and not self.commands.full():
                    self.commands.put_nowait(command)
                if self.social.interested(text) and not self.social.inbox.full():
                    self.social.inbox.put_nowait((uid, text))

    async def management_command(self, uid, verified, cmd, query):
        error = self.permissions.authorization_error(uid, verified, cmd)
        if error:
            await self.say(error)
            return
        if cmd in {"!admins", "!blocked"}:
            ids = self.permissions.admins if cmd == "!admins" else self.permissions.blocked
            for target in sorted(ids)[:12]:
                try:
                    data = await self.api("GET", f"/user/user-{target}")
                    info = data.get("denormalized", {}).get(API + f"/user/user-{target}", {}).get("data", {})
                    name = info.get("username")
                    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", name):
                        self.permissions.names[target] = name
                except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
                    pass  # Fall back to saved usernames when lookup is unavailable.
            await self.say(self.permissions.manage(uid, verified, cmd))
            return
        try:
            target, username = await resolve_username(self.api, query)
        except UserLookupError as error:
            await self.say(str(error))
            return
        await self.say(await asyncio.to_thread(self.permissions.manage, uid, verified, cmd, target, username=username))

    async def command_worker(self):
        while True:
            uid, text, verified = await self.commands.get()
            try:
                cmd, _, query = text.partition(" ")
                cmd = ALIASES.get(cmd.lower(), cmd.lower())
                if not self.permissions.allowed(uid):
                    continue
                if cmd not in {"!play", "!queue", "!np", "!skip", "!stop", "!help"} | MANAGEMENT_COMMANDS:
                    continue
                now = time.monotonic()
                if now - self.cooldowns.get(uid, -1000) < 5:
                    continue
                self.cooldowns[uid] = now
                if len(self.cooldowns) > 500:
                    self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < 60}
                if cmd in MANAGEMENT_COMMANDS:
                    await self.management_command(uid, verified, cmd, query.strip())
                elif cmd == "!play":
                    if not query.strip():
                        await self.say("Usage: !play <song name or YouTube link>")
                        continue
                    if self.radio.queue.full():
                        await self.say("The music queue is full. Please try later.")
                        continue
                    track = await self.radio.lookup(query.strip())
                    if uid not in self.members or not self.permissions.allowed(uid):
                        continue
                    self.radio.queue.put_nowait(track)
                    if self.radio.idle:
                        self.radio.interrupt()
                    await self.say("Queued: " + track["title"])
                elif cmd in {"!skip", "!stop"}:
                    if cmd == "!stop":
                        while not self.radio.queue.empty():
                            self.radio.queue.get_nowait()
                            self.radio.queue.task_done()
                    self.radio.interrupt()
                    await self.say("Music stopped." if cmd == "!stop" else "Skipping track.")
                elif cmd == "!np":
                    await self.say("Now playing: " + self.radio.current["title"] if self.radio.current else "No song is playing.")
                elif cmd == "!queue":
                    # Queue inspection happens on the same asyncio thread.
                    titles = [t["title"] for t in list(self.radio.queue._queue)[:5]]
                    await self.say("Next: " + " | ".join(titles) if titles else "The queue is empty.")
                else:
                    await self.say("🎶 !play اسم الأغنية | !queue | !np | 🎲 !game !guess !joke !dare !fortune !scores | ناديني رامسي للدردشة 😎")
            except (RuntimeError, ValueError, asyncio.TimeoutError, aiohttp.ClientError, ConnectionError):
                LOG.warning("A command failed; no credentials or media URLs logged.")
                with contextlib.suppress(ConnectionError):
                    await self.say("Could not complete that request. Try another song or check the server logs.")
            finally:
                self.commands.task_done()

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers={"X-Imvu-Application": "welcome/1",
                "Origin": "https://www.imvu.com", "Accept": "application/json"}) as self.session:
            await self.login()
            delay = 5
            while True:
                try:
                    await self.api("POST", f"/chat/chat-{self.room}/participants", json={})
                    data = await self.api("GET", f"/chat/chat-{self.room}")
                    info = data.get("denormalized", {}).get(API + f"/chat/chat-{self.room}", {}).get("data", {})
                    self.queue = info.get("imq_queue")
                    if not self.queue:
                        raise RuntimeError("IMVU did not return a room queue.")
                    await self.participants()
                    async with self.session.ws_connect(IMQ, origin="https://www.imvu.com", timeout=aiohttp.ClientWSTimeout(ws_receive=60)) as self.ws:
                        await self.ws.send_json({"record": "msg_c2g_connect", "op_id": 0,
                            "user_id": self.user_id, "cookie": base64.b64encode(self.cookie.value.encode()).decode(),
                            "metadata": [{"record": "metadata", "key": "app", "value": "aW12dV9uZXh0"},
                                         {"record": "metadata", "key": "platform_type", "value": "Ymln"}]})
                        auth = await self.ws.receive_json(timeout=20)
                        if auth.get("record") != "msg_g2c_result" or auth.get("op_id") != 0 or auth.get("status") != 0:
                            raise RuntimeError("IMQ authentication rejected or response changed.")
                        await self.packet("msg_c2g_open_floodgates")
                        for queue in (self.queue, f"inv:/chat/chat-{self.room}"):
                            await self.packet("msg_c2g_subscribe", queues_with_results=[{"record": "subscription", "name": queue, "op_id": self.op + 1}])
                        self.connected = True
                        LOG.info("Ramsei connected to room %s; live chat validation still required", self.room)
                        delay = 5
                        tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.presence())]
                        try:
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            self.connected = False
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                except AuthenticationRequired:
                    self.connected = False
                    LOG.warning("Saved login expired; trying normal IMVU login with configured verification")
                    await self.login(force=True)
                except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, ValueError) as error:
                    detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                    LOG.warning("Room connection lost (%s); retrying in %s seconds", detail, delay)
                finally:
                    self.connected = False
                while not self.commands.empty():
                    self.commands.get_nowait()
                    self.commands.task_done()
                while not self.social.inbox.empty():
                    self.social.inbox.get_nowait()
                    self.social.inbox.task_done()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    radio = Radio()
    bot = Bot(radio)
    app = web.Application()
    async def health(request):
        return web.json_response({"status": "connected" if bot.connected else "not_connected",
            "room": bot.room, "playing": radio.current["title"] if radio.current else None,
            "queued": radio.queue.qsize(), "listeners": len(radio.listeners)})
    app.router.add_get("/health", health)
    app.router.add_get("/stream.mp3", radio.stream)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "3000"))).start()
    if not os.getenv("DATABASE_URL", "").strip():
        LOG.warning("Local state only: Bothost rebuilds erase saved login, admins and blocks. Set DATABASE_URL for durable storage.")
    LOG.info("Radio endpoint /stream.mp3; diagnostic status /health")
    tasks = [asyncio.create_task(bot.run()), asyncio.create_task(bot.command_worker()),
             asyncio.create_task(radio.run(bot.say)), asyncio.create_task(bot.social.run())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Deliberately avoid tracebacks containing authentication or signed media URLs.
        LOG.error("Startup stopped: %s", str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        sys.exit(1)
