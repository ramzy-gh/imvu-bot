"""RAMSEI v11: named wardrobe outfit Bot, AI fallback, and English welcomes."""
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from aiohttp import web
from yarl import URL
from personality import ALIASES, Social
from permissions import Permissions, MANAGEMENT_COMMANDS, ROOM_COMMANDS, normalize_management_message
from identity import resolve_username, UserLookupError
from state_store import StateStore
from wardrobe import load_named_outfit, OutfitError
from movement import Movement, legacy_seat_message

LOG = logging.getLogger("ramsei")
API = "https://api.imvu.com"
IMQ = "wss://wss-imq.imvu.com/streaming/imvu_pre"


class AuthenticationRequired(RuntimeError):
    pass


class IMVURequestError(RuntimeError):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f"IMVU request failed: HTTP {status} at {path.split('?')[0]}")


class LoginPaused(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def encode(value):
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def transport_user_id(value):
    """IMQ encodes sender IDs as base64; support already-decoded numeric IDs too."""
    text = str(value)
    if re.fullmatch(r"\d+", text):
        return text
    try:
        decoded = base64.b64decode(text, validate=True).decode("ascii")
        return decoded if re.fullmatch(r"\d+", decoded) else None
    except (ValueError, UnicodeError):
        return None


def appearance_products(data, uid):
    node = data.get("denormalized", {}).get(API + f"/profile_outfit/profile_outfit-{uid}", {})
    look = node.get("data", {}).get("look_url", "")
    if not isinstance(look, str):
        raise ValueError("Saved profile has no supported look URL")
    parsed = urlparse(look)
    if parsed.scheme != "https" or parsed.netloc != "api.imvu.com" or not parsed.path.startswith("/look/"):
        raise ValueError("Saved profile has no supported look URL")
    values = unquote(parsed.path[len("/look/"):]).split(",")
    if not values or len(values) > 100 or any(not re.fullmatch(r"[1-9]\d*", v) for v in values):
        raise ValueError("Saved profile has invalid product IDs")
    products = list(dict.fromkeys(values))
    if len(" ".join(products)) > 400:
        raise ValueError("Saved outfit exceeds supported announcement length")
    return products


def participant_ids(data, collection_url):
    """Read membership from participant nodes and authoritative collection items."""
    nodes = data.get("denormalized", {})
    found = set()
    for url in nodes:
        match = re.fullmatch(re.escape(collection_url) + r"/user-(\d+)(?:[/?#].*)?", url)
        if match:
            found.add(match[1])
    collection = nodes.get(collection_url, {}).get("data", {})
    for item in collection.get("items", []):
        url = item.get("id", "") if isinstance(item, dict) else item
        if not isinstance(url, str):
            continue
        match = re.fullmatch(r"https://api\.imvu\.com/(?:user/|chat/chat-[0-9-]+/participants/)user-(\d+)(?:[/?#].*)?", url)
        if match:
            found.add(match[1])
    return found


def rejection_reason(packet, queue, members, own_id):
    if packet.get("record") != "msg_g2c_send_message":
        return "not_message"
    if packet.get("queue") != queue:
        return "other_queue"
    if packet.get("mount") != "messages":
        return "other_mount"
    try:
        payload = json.loads(base64.b64decode(packet.get("message", ""), validate=True))
        if not isinstance(payload, dict):
            return "payload_not_object"
        if "userId" not in payload or "message" not in payload:
            return "payload_fields_changed"
        sender = str(payload["userId"])
        if sender == own_id:
            return "self_message"
        if sender not in members:
            return "sender_missing_from_participants"
        if payload.get("to", 0) not in (0, "0", None):
            return "private_or_unknown_recipient"
        return "system_or_sender_validation"
    except (ValueError, TypeError, UnicodeError):
        return "payload_encoding_changed"


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
        if verified is not None and transport_user_id(verified) != sender:
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
        self.last_error = None
        self.bytes_sent = 0
        self.pcm = asyncio.Queue(maxsize=5)
        self.encoder_starts = 0
        self.track_starts = 0
        self.recent_frames = deque(maxlen=10)  # About 260 ms for decoder startup.
        self.listener_resyncs = 0
        self.source_retries = 0

    @staticmethod
    def lookup_target(query):
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
        return target

    async def search(self, query):
        target = self.lookup_target(query)
        if not target.startswith("ytsearch1:"):
            raise ValueError("Search using a song or artist name.")
        data = json.loads(await capture(sys.executable, "-m", "yt_dlp", "--no-warnings",
            "--flat-playlist", "--skip-download", "--dump-single-json",
            "--socket-timeout", "15", "--retries", "1", "--", "ytsearch5:" + query))
        choices, seen = [], set()
        for item in data.get("entries", []) or []:
            if not isinstance(item, dict):
                continue
            video_id = item.get("id", "")
            duration = item.get("duration")
            if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                    or video_id in seen or item.get("is_live")
                    or item.get("live_status") in {"is_live", "is_upcoming", "post_live"}
                    or (isinstance(duration, (int, float)) and (duration <= 0 or duration > 900))):
                continue
            seen.add(video_id)
            choices.append({"title": " ".join(str(item.get("title") or "Unknown track").split())[:140],
                            "url": "https://www.youtube.com/watch?v=" + video_id})
            if len(choices) == 5:
                break
        if not choices:
            raise ValueError("No song choices found. Try a more specific song name.")
        return choices

    async def lookup(self, query):
        target = self.lookup_target(query)
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
                "url": data["url"], "duration": data["duration"],
                "source": data.get("webpage_url") or target}

    def interrupt(self):
        self.skip.set()
        while not self.pcm.empty():
            self.pcm.get_nowait()

    async def broadcast(self, chunk):
        if chunk is not None:
            self.bytes_sent += len(chunk)
            self.recent_frames.append(chunk)
        for listener in tuple(self.listeners):
            if listener.full():
                # Classic may stay ON AIR after EOF without reconnecting. Drop
                # stale complete MP3 frames, not the listener's HTTP connection.
                while not listener.empty():
                    listener.get_nowait()
                self.listener_resyncs += 1
            listener.put_nowait(chunk)

    async def stream(self, request):
        if request.method != "HEAD" and len(self.listeners) >= 50:
            raise web.HTTPServiceUnavailable(text="Listener limit reached")
        response = web.StreamResponse(headers={"Content-Type": "audio/mpeg",
            "Cache-Control": "no-store, no-cache, must-revalidate, no-transform",
            "Pragma": "no-cache", "Expires": "0", "Accept-Ranges": "none",
            "X-Accel-Buffering": "no", "icy-name": "RAMSEI Radio", "icy-br": "128"})
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        listener = asyncio.Queue(maxsize=64)
        for frame in self.recent_frames:
            listener.put_nowait(frame)
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

    def decoder_args(self, track):
        return [os.getenv("FFMPEG_BINARY", "ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "https,tls,tcp,crypto", "-rw_timeout", "15000000",
                "-reconnect", "1", "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "429,5xx", "-reconnect_delay_max", "5",
                "-ss", str(track.get("offset", 0)), "-i", track["url"],
                "-t", str(max(0.1, 900 - track.get("offset", 0))), "-vn", "-ac", "2", "-ar", "44100",
                "-f", "s16le", "pipe:1"]

    @staticmethod
    async def stop_process(proc):
        if proc:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            try:
                await asyncio.wait_for(proc.communicate(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.communicate()

    async def produce_pcm(self, announce):
        """Decode each song; the encoder continues receiving silence during gaps."""
        while True:
            self.idle = True
            self.current = None
            track = await self.queue.get()
            self.current = track
            self.idle = False
            self.skip.clear()
            stop_task = None
            try:
                self.track_starts += 1
                stop_task = asyncio.create_task(self.skip.wait())
                with contextlib.suppress(ConnectionError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
                    await announce("Now playing: " + track["title"])
                await self.decode_track(track, stop_task)
            except (OSError, ValueError):
                self.last_error = "Audio decoder could not start; check FFmpeg and the song source."
                LOG.warning(self.last_error)
            finally:
                if stop_task:
                    stop_task.cancel()
                    await asyncio.gather(stop_task, return_exceptions=True)
                await self.stop_process(self.proc)
                self.proc = None
                self.current = None
                self.idle = True
                self.queue.task_done()

    async def decode_track(self, track, stop_task):
        """Resume truncated sources at the number of PCM samples already queued."""
        decoded_bytes = 0
        self.last_error = None
        for attempt in range(4):
            if self.skip.is_set():
                return
            source = dict(track, offset=decoded_bytes / 176400)
            read_task = None
            try:
                self.proc = await asyncio.create_subprocess_exec(*self.decoder_args(source),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                while not self.skip.is_set():
                    read_task = asyncio.create_task(self.proc.stdout.readexactly(17640))
                    done, _ = await asyncio.wait({read_task, stop_task}, timeout=45,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if self.skip.is_set():
                        return
                    if read_task not in done:
                        break  # Bounded stall recovery; keep the radio connection alive.
                    ended = False
                    try:
                        chunk = read_task.result()
                    except asyncio.IncompleteReadError as error:
                        chunk, ended = error.partial, True
                    if chunk:
                        await self.pcm.put(chunk.ljust(17640, b"\0"))
                        decoded_bytes += len(chunk)
                    if ended:
                        await self.proc.wait()
                        break
            finally:
                if read_task:
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                returncode = self.proc.returncode if self.proc else None
                await self.stop_process(self.proc)
                self.proc = None
            if self.skip.is_set():
                return
            position = decoded_bytes / 176400
            duration = min(float(track.get("duration", 900)), 900)
            # Extractor durations can round down by a second. Clean EOF near the
            # expected end is normal; clean EOF far before it is truncation too.
            if (position > 0 and returncode == 0 and position >= duration - 1) or position >= duration or (returncode == 0 and "duration" not in track):
                return
            if attempt == 3:
                self.last_error = "Song source ended early after three resume attempts."
                LOG.warning("Audio source incomplete: decoded=%.1fs expected=%.1fs", position, duration)
                return
            self.source_retries += 1
            LOG.warning("Resuming interrupted audio source at %.1fs (attempt %s)", position, attempt + 1)
            # Fresh URLs also recover tracks whose signed link expired in queue.
            if track.get("source"):
                refresh = asyncio.create_task(self.lookup(track["source"]))
                try:
                    done, _ = await asyncio.wait({refresh, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                    if self.skip.is_set():
                        return
                    track = dict(track, url=refresh.result()["url"])
                except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                    pass  # The original URL may still recover on the next attempt.
                finally:
                    refresh.cancel()
                    await asyncio.gather(refresh, return_exceptions=True)
            try:
                await asyncio.wait_for(self.skip.wait(), 1)
                return
            except asyncio.TimeoutError:
                pass

    async def run(self, announce):
        """One continuous MP3 encoder per room, across all tracks and idle silence."""
        while True:
            encoder = None
            tasks = []
            try:
                encoder = await asyncio.create_subprocess_exec(os.getenv("FFMPEG_BINARY", "ffmpeg"),
                    "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "s16le",
                    "-ar", "44100", "-ac", "2", "-probesize", "32", "-analyzeduration", "0", "-i", "pipe:0", "-vn", "-c:a", "libmp3lame",
                    "-b:a", "128k", "-reservoir", "0", "-write_xing", "0", "-id3v2_version", "0",
                    "-flush_packets", "1", "-f", "mp3", "pipe:1",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                self.encoder_starts += 1

                async def feed():
                    deadline = asyncio.get_running_loop().time()
                    while True:
                        try:
                            chunk = self.pcm.get_nowait()
                        except asyncio.QueueEmpty:
                            chunk = bytes(17640)  # 100 ms of 44.1 kHz stereo s16 PCM
                        encoder.stdin.write(chunk)
                        await encoder.stdin.drain()
                        deadline += 0.1
                        now = asyncio.get_running_loop().time()
                        if deadline < now:
                            deadline = now
                        await asyncio.sleep(max(0, deadline - now))

                async def pump():
                    # Our encoder emits MPEG-1 Layer III, 128 kb/s, 44.1 kHz.
                    # Keep frame boundaries so resyncs and late joins never
                    # start halfway through a frame (bit reservoir is disabled).
                    while True:
                        try:
                            header = await encoder.stdout.readexactly(4)
                            bits = int.from_bytes(header, "big")
                            if (bits & 0xFFFEFC00) != 0xFFFA9000:
                                raise RuntimeError("Unexpected MP3 encoder format")
                            frame_size = 144000 * 128 // 44100 + ((bits >> 9) & 1)
                            frame = header + await encoder.stdout.readexactly(frame_size - 4)
                        except asyncio.IncompleteReadError as error:
                            raise RuntimeError("Continuous audio encoder stopped") from error
                        await self.broadcast(frame)

                tasks = [asyncio.create_task(coro) for coro in (feed(), pump(), self.produce_pcm(announce))]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except (OSError, RuntimeError, ConnectionError):
                self.last_error = "Continuous audio encoder failed; check FFmpeg installation. Retrying."
                LOG.warning(self.last_error)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.stop_process(encoder)
                self.recent_frames.clear()
                while not self.pcm.empty():
                    self.pcm.get_nowait()
            await asyncio.sleep(2)


class Bot:
    def __init__(self, radio, room=None, permissions=None, authentication=None, room_manager=None):
        self.radio = radio
        self.room = room or os.getenv("IMVU_ROOM_ID", "330286535-177")
        self.authentication = authentication
        self.room_manager = room_manager
        if not re.fullmatch(r"\d+-\d+", self.room):
            raise ValueError("IMVU_ROOM_ID must look like 330286535-177")
        self.permissions = permissions if permissions is not None else Permissions()
        self.members = set()
        self.greeted = OrderedDict()
        self.pending_welcomes = OrderedDict()
        self.members_initialized = False
        self.welcome_lock = asyncio.Lock()
        self.movement = Movement(self)
        self.last_seat_announcement = -1000
        self.cooldowns = {}
        self.song_choices = OrderedDict()
        self.commands = asyncio.Queue(maxsize=50)
        self.ws = None
        self.connected = False
        self.status = "starting"
        self.status_message = ""
        self.authenticated = asyncio.Event()
        self.chat_ready = False
        self.announced = False
        self.room_subscription_op = None
        self.pending_chat_ops = set()
        self.diagnostics = {"packets_received": 0, "message_packets": 0,
            "accepted_messages": 0, "commands_received": 0, "messages_sent": 0,
            "messages_acked": 0, "last_rejection": "none"}
        self.last_diagnostic = 0
        self.last_membership_refresh = 0
        self.last_account_presence = 0
        self.account_presence = {"state": "not_updated", "online": None}
        self.appearance = {"state": "not_announced", "product_count": 0}
        self.outfit_products = []
        self.last_appearance = 0
        self.appearance_pending = False
        self.outfit_loaded_at = 0
        self.next_outfit_attempt = 0
        self.outfit_name = os.getenv("IMVU_OUTFIT_NAME", "Bot").strip() or "Bot"
        self.session = None
        self.op = 0
        self.last_said = 0
        self.say_lock = asyncio.Lock()
        self.social = Social(self.say, allowed=self.permissions.allowed)

    async def api(self, method, path, **kwargs):
        async with self.session.request(method, API + path, **kwargs) as response:
            if response.status == 401:
                raise AuthenticationRequired("IMVU session expired; a fresh password login is required.")
            if response.status not in (200, 201, 204):
                raise IMVURequestError(response.status, path)
            return await response.json() if response.status != 204 else {}

    def session_path(self):
        return Path(os.getenv("SESSION_PATH", "/app/data/ramsei/imvu-session.json"))

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
            LOG.info("Authenticated session saved")
        except OSError:
            if os.getenv("DATABASE_URL", "").strip():
                raise RuntimeError("Could not save the session to the external database.") from None
            LOG.warning("Session could not be saved; ensure SESSION_PATH points to writable persistent storage")

    async def login(self, force=False):
        if self.authentication is not None:
            await self.authentication.login(self, force)
        else:
            await self._login(force)

    async def _login(self, force=False):
        if not force and self.restore_session():
            return
        username = os.getenv("IMVU_USERNAME", "Ramsei")
        password = os.getenv("IMVU_PASSWORD", "")
        if not password:
            raise RuntimeError("Set IMVU_PASSWORD privately in Bothost environment variables.")
        payload = {"username": username, "password": password, "gdpr_cookie_acceptance": False}
        async with self.session.post(API + "/login", json=payload) as response:
            if response.status == 202:
                raise LoginPaused("verification_required", "IMVU requires additional verification. V6 supports password-only login and cannot complete this challenge. Login is paused; no automatic retries. Resolve account verification through IMVU before restarting.")
            if response.status == 429:
                retry_after = response.headers.get("Retry-After", "")
                wait = f" Server requests a wait of {retry_after} seconds." if retry_after.isdigit() else ""
                raise LoginPaused("rate_limited", "IMVU rate-limited login (HTTP 429). Login is paused; no automatic retries." + wait)
            if response.status not in (200, 201):
                raise LoginPaused("login_rejected", f"Login rejected: HTTP {response.status}. Check credentials/verification privately. Login is paused; restart only after resolving it.")
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

    async def login_or_pause(self, force=False):
        """Keep the web process alive on auth failure so hosting cannot restart-loop login."""
        try:
            await self.login(force=force)
        except (LoginPaused, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as error:
            self.connected = False
            self.authenticated.clear()
            self.status = error.status if isinstance(error, LoginPaused) else "login_paused"
            self.status_message = str(error) if isinstance(error, RuntimeError) else "Login could not complete. Check configuration/network, then restart once."
            LOG.warning("%s", self.status_message)
            await asyncio.Event().wait()  # Only an explicit process restart attempts login again.
            return
        self.status = "room_connecting"
        self.status_message = ""
        self.authenticated.set()

    async def packet(self, record, **fields):
        if self.ws is None or self.ws.closed:
            raise ConnectionError("IMVU chat is disconnected")
        self.op += 1
        payload = {"record": record, **fields}
        # These control records have no top-level operation field in IMQ.
        # Subscribe acknowledgement IDs live inside queues_with_results instead.
        if record == "msg_c2g_send_message":
            payload["op_id"] = self.op
        await self.ws.send_json(payload)
        return self.op

    async def say(self, text):
        if not self.connected:
            return False
        async with self.say_lock:
            await asyncio.sleep(max(0, self.last_said + 1.5 - time.monotonic()))
            if not self.connected:
                return False
            operation = await self.packet("msg_c2g_send_message", queue=self.queue, mount="messages",
                message=encode({"chatId": self.queue.rsplit("/", 1)[-1], "message": text[:450],
                                "to": 0, "userId": self.user_id}))
            self.pending_chat_ops.add(operation)
            # Outstanding acknowledgements are diagnostic only; keep memory bounded.
            if len(self.pending_chat_ops) > 100:
                self.pending_chat_ops.discard(min(self.pending_chat_ops))
            self.diagnostics["messages_sent"] += 1
            self.last_said = time.monotonic()
            bridge = getattr(self.room_manager, "telegram", None)
            if bridge:
                bridge.record(self.room, self.user_id, text, os.getenv("IMVU_USERNAME", "Ramsei"))
            return True

    async def participants(self, greet=False):
        path = f"/chat/chat-{self.room}/participants"
        data = await self.api("GET", path)
        new = participant_ids(data, API + path)
        self.last_membership_refresh = time.monotonic()
        if self.members_initialized:
            if new - self.members - {self.user_id}:
                self.appearance_pending = True
            for uid in sorted(new - self.members - {self.user_id}):
                self.pending_welcomes[uid] = time.monotonic()
        self.members_initialized = True
        # Update membership before profile lookups or chat sends. Even a refresh
        # from receive() must retain arrivals for the next greeting pass.
        self.members = new
        self.pending_welcomes = OrderedDict((uid, joined) for uid, joined in self.pending_welcomes.items()
                                           if uid in new and time.monotonic() - joined < 120)
        while len(self.pending_welcomes) > 500:
            self.pending_welcomes.popitem(last=False)
        if not greet:
            LOG.info("Membership loaded: %s participants recognized", len(new))
        if greet:
            await self.send_welcomes()

    async def send_welcomes(self):
        async with self.welcome_lock:
            if not self.connected or not self.chat_ready:
                return
            if self.pending_welcomes:
                with contextlib.suppress(RuntimeError, ValueError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
                    await self.announce_seat()
            for uid in list(self.pending_welcomes):
                if uid not in self.members or uid == self.user_id or not self.permissions.allowed(uid):
                    self.pending_welcomes.pop(uid, None)
                    continue
                if time.monotonic() - self.greeted.get(uid, -1000) < 300:
                    self.pending_welcomes.pop(uid, None)
                    continue
                try:
                    user = await self.api("GET", f"/user/user-{uid}")
                    info = user.get("denormalized", {}).get(API + f"/user/user-{uid}", {}).get("data", {})
                    name = info.get("username")
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,40}", name):
                        continue
                    if uid not in self.members or not self.permissions.allowed(uid):
                        self.pending_welcomes.pop(uid, None)
                        continue
                    sent = await self.say(f"Welcome, @{name}! Great to have you here! Music: !play <song or artist> | Games: !game")
                    if not sent:
                        continue
                    self.pending_welcomes.pop(uid, None)
                    self.greeted[uid] = time.monotonic()
                    self.greeted.move_to_end(uid)
                    while len(self.greeted) > 500:
                        self.greeted.popitem(last=False)
                except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, ValueError):
                    # Retry this greeting later without disconnecting the room
                    # or preventing other arrivals from being welcomed.
                    LOG.warning("Welcome deferred in room %s; will retry.", self.room)

    async def announce_seat(self):
        if not self.connected or not self.chat_ready or time.monotonic() - self.last_seat_announcement < 5:
            return
        data = await self.api("GET", f"/chat/chat-{self.room}/participants/user-{self.user_id}")
        if await self.say(legacy_seat_message(data, self.room, self.user_id)):
            self.last_seat_announcement = time.monotonic()

    async def update_account_presence(self):
        try:
            data = await self.api("POST", f"/presence/presence-{self.user_id}", json={"online": True})
            online = data.get("denormalized", {}).get(API + f"/presence/presence-{self.user_id}", {}).get("data", {}).get("online")
            self.account_presence = {"state": "updated", "online": online if isinstance(online, bool) else None}
            self.last_account_presence = time.monotonic()
        except AuthenticationRequired:
            raise
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            self.account_presence = {"state": "update_failed", "online": None}
            LOG.warning("Account presence update failed; will retry on the next heartbeat")

    async def presence(self):
        last_keepalive = -1000
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            if now - last_keepalive >= 15:
                await self.packet("msg_c2g_ping")
                if await self.room_maintenance(self.api("POST", f"/chat/chat-{self.room}/participants/user-{self.user_id}", json={})):
                    last_keepalive = now
            if now - self.last_account_presence >= 45:
                await self.update_account_presence()
            # Seat announcements are for arrival/movement. Re-sending an
            # unchanged seat on a timer can make Classic rebuild the avatar.
            await self.room_maintenance(self.participants(greet=True))
            if self.appearance_pending:
                await self.announce_appearance()

    async def room_maintenance(self, operation):
        """An API hiccup must not tear down a healthy IMQ socket and reload the avatar."""
        try:
            await operation
            return True
        except IMVURequestError as error:
            if error.status not in (408, 429) and error.status < 500:
                raise  # Missing membership or denied access needs real recovery.
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            pass
        LOG.warning("Room maintenance deferred in %s; keeping the chat connection", self.room)
        return False

    async def announce_appearance(self):
        if time.monotonic() - self.last_appearance < 10 or time.monotonic() < self.next_outfit_attempt:
            return
        try:
            if not self.outfit_products or time.monotonic() - self.outfit_loaded_at > 300:
                selected = await load_named_outfit(self.api, self.user_id, self.outfit_name)
                self.outfit_products = selected["products"]
                self.outfit_loaded_at = time.monotonic()
            ids = " ".join(self.outfit_products)
            # Announce only the named outfit from this account's own wardrobe.
            # No room/global settings, seats, or other avatars are modified.
            if not await self.say("*putOnOutfit " + ids):
                return
            await self.announce_seat()
            self.last_appearance = time.monotonic()
            self.appearance_pending = False
            self.appearance = {"state": "announced_not_render_verified", "product_count": len(self.outfit_products),
                                 "source": "named_saved_outfit", "name": self.outfit_name}
            LOG.info("Named saved outfit selected and announced: %s products; client rendering requires visual verification", len(self.outfit_products))
        except (RuntimeError, ValueError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as error:
            self.appearance_pending = True
            self.next_outfit_attempt = time.monotonic() + 60
            reason = str(error) if isinstance(error, OutfitError) else type(error).__name__
            self.appearance = {"state": "announcement_failed", "product_count": 0,
                               "source": "named_saved_outfit", "name": self.outfit_name, "reason": reason}
            LOG.warning("Named outfit not announced: reason=%s; no profile/default substitution", reason)

    async def confirm_chat_ready(self):
        if self.chat_ready:
            return
        self.chat_ready = True
        LOG.info("Room chat subscription confirmed; checking message delivery")
        await self.announce_appearance()
        if not self.announced:
            self.announced = True
            await self.say("Ramsei is here! رامسي معاكم يا نجوم 😎💛 Try !help / !game")

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
            if not isinstance(packet, dict):
                continue
            self.diagnostics["packets_received"] += 1
            operation = packet.get("op_id")
            if isinstance(operation, str) and operation.isdigit():
                operation = int(operation)
            if packet.get("record") == "msg_g2c_result" and packet.get("status", 0) != 0:
                purpose = "send_chat" if operation in self.pending_chat_ops else "subscribe_or_control"
                LOG.warning("IMVU rejected operation: purpose=%s status=%s", purpose, packet.get("status"))
                raise RuntimeError("IMVU rejected a chat operation; see purpose/status diagnostic.")
            if packet.get("record") == "msg_g2c_result" and packet.get("status") == 0:
                if operation in self.pending_chat_ops:
                    self.pending_chat_ops.discard(operation)
                    self.diagnostics["messages_acked"] += 1
                    LOG.info("IMVU acknowledged outgoing chat message")
                if operation == self.room_subscription_op and not self.chat_ready:
                    await self.confirm_chat_ready()
            if packet.get("record") == "msg_g2c_create_mount" and packet.get("queue") == self.queue and packet.get("mount") == "messages":
                await self.confirm_chat_ready()
            if packet.get("record") == "msg_g2c_send_message":
                self.diagnostics["message_packets"] += 1
                if packet.get("queue") == self.queue and packet.get("mount") == "messages":
                    await self.confirm_chat_ready()
            command = decode_message(packet, self.queue, self.members, self.user_id)
            if command is None and rejection_reason(packet, self.queue, self.members, self.user_id) == "sender_missing_from_participants":
                if time.monotonic() - self.last_membership_refresh >= 5:
                    await self.room_maintenance(self.participants())
                    command = decode_message(packet, self.queue, self.members, self.user_id)
            if command is None and packet.get("record") == "msg_g2c_send_message":
                reason = rejection_reason(packet, self.queue, self.members, self.user_id)
                if reason != "self_message":
                    self.diagnostics["last_rejection"] = reason
                    if time.monotonic() - self.last_diagnostic >= 10:
                        LOG.info("Chat diagnostic: reason=%s members=%s packets=%s", reason, len(self.members), self.diagnostics["message_packets"])
                        self.last_diagnostic = time.monotonic()
            if command:
                self.diagnostics["accepted_messages"] += 1
                uid, text, verified = command
                bridge = getattr(self.room_manager, "telegram", None)
                if bridge:
                    # Relay public chat before feature blocks or command aliases
                    # change it. Private whispers and system actions are excluded
                    # by decode_message, and own messages are relayed by say().
                    bridge.record(self.room, uid, text)
                text = normalize_management_message(text)
                command = uid, text, verified
                if not self.permissions.allowed(uid):
                    continue
                picking_song = uid in self.song_choices and self.is_song_number(text)
                if (text.startswith("!") or picking_song) and not self.commands.full():
                    self.diagnostics["commands_received"] += 1
                    LOG.info("Room command received and queued")
                    self.commands.put_nowait(command)
                if not picking_song and self.social.interested(text) and not self.social.inbox.full():
                    self.social.inbox.put_nowait((uid, text))

    async def management_command(self, uid, verified, cmd, query):
        error = self.permissions.authorization_error(uid, verified, cmd)
        if error:
            await self.say(error)
            return
        if cmd in {"!admins", "!blocked", "!roomadmins"}:
            ids = {"!admins": self.permissions.admins, "!blocked": self.permissions.blocked,
                   "!roomadmins": self.permissions.room_admins}[cmd]
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

    @staticmethod
    def is_song_number(text):
        return re.fullmatch(r"[0-9٠-٩]{1,2}", text.strip()) is not None

    async def queue_song(self, uid, url):
        if uid not in self.members or not self.permissions.allowed(uid):
            return False
        if self.radio.queue.full():
            await self.say("The music queue is full. Please try later.")
            return False
        track = await self.radio.lookup(url)
        if uid not in self.members or not self.permissions.allowed(uid):
            return False
        if self.radio.queue.full():
            await self.say("The music queue is full. Please try later.")
            return False
        self.radio.queue.put_nowait(track)
        if self.radio.idle:
            self.radio.interrupt()
        await self.say("Queued: " + track["title"])
        if not self.radio.listeners:
            await self.say("No radio listeners connected. Use !radio for the stream setup.")
        return True

    async def pick_song(self, uid, text):
        pending = self.song_choices.get(uid)
        if pending is None:
            return
        expires, choices = pending
        if time.monotonic() >= expires:
            self.song_choices.pop(uid, None)
            await self.say("Your song choices expired. Search again with !play <song or artist>.")
            return
        number = int(text.strip())
        if not 1 <= number <= len(choices):
            await self.say(f"Pick a number from 1 to {len(choices)} for your song search.")
            return
        if await self.queue_song(uid, choices[number - 1]["url"]):
            self.song_choices.pop(uid, None)

    async def play_request(self, uid, query):
        self.song_choices.pop(uid, None)
        if not query:
            await self.say("Usage: !play <song name, artist or YouTube link>")
            return
        if self.radio.queue.full():
            await self.say("The music queue is full. Please try later.")
            return
        target = self.radio.lookup_target(query)
        if not target.startswith("ytsearch1:"):
            await self.queue_song(uid, query)
            return
        choices = await self.radio.search(query)
        if uid not in self.members or not self.permissions.allowed(uid):
            return
        if len(choices) == 1:
            await self.queue_song(uid, choices[0]["url"])
            return
        now = time.monotonic()
        self.song_choices = OrderedDict((key, value) for key, value in self.song_choices.items() if value[0] > now)
        self.song_choices[uid] = (now + 120, choices)
        while len(self.song_choices) > 100:
            self.song_choices.popitem(last=False)
        title = " ".join(query.split())[:40]
        lines = [f'Song choices for "{title}":']
        lines.extend(f'{number}. {choice["title"][:52]}' for number, choice in enumerate(choices, 1))
        lines.append(f"Requester: reply 1-{len(choices)} within 2 minutes.")
        await self.say("\n".join(lines))

    async def command_worker(self):
        while True:
            uid, text, verified = await self.commands.get()
            try:
                text = normalize_management_message(text)
                parts = text.split(maxsplit=1)
                cmd = parts[0] if parts else ""
                query = parts[1] if len(parts) > 1 else ""
                cmd = ALIASES.get(cmd.lower(), cmd.lower())
                if not self.permissions.allowed(uid):
                    continue
                if self.is_song_number(text) and uid in self.song_choices:
                    # A selection must work immediately after its search; the
                    # normal five-second command cooldown does not apply.
                    await self.pick_song(uid, text)
                    continue
                if cmd not in {"!play", "!queue", "!np", "!skip", "!stop", "!help", "!radio", "!move"} | MANAGEMENT_COMMANDS | ROOM_COMMANDS:
                    continue
                now = time.monotonic()
                if now - self.cooldowns.get(uid, -1000) < 5:
                    continue
                self.cooldowns[uid] = now
                if len(self.cooldowns) > 500:
                    self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < 60}
                if cmd in ROOM_COMMANDS:
                    if self.room_manager is None:
                        await self.say("Room controls are unavailable in this process.")
                    else:
                        await self.room_manager.chat_command(self, uid, verified, cmd, query.strip())
                elif cmd in MANAGEMENT_COMMANDS:
                    await self.management_command(uid, verified, cmd, query.strip())
                elif cmd == "!radio":
                    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
                    path = f"/rooms/{self.room}/stream.mp3"
                    await self.say("Host/mod: load " + (base + path if base else "your HTTPS host domain + " + path) + " in IMVU Radio, then press Play. Listeners: " + str(len(self.radio.listeners)))
                elif cmd == "!play":
                    await self.play_request(uid, query.strip())
                elif cmd == "!move":
                    if query.strip():
                        await self.say("Type !move by itself and I'll look for a free spot near you.")
                    else:
                        reply = await self.movement.move_near(uid)
                        if reply:
                            await self.say(reply)
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
                    await self.say("🎶 !play <song / اسم الأغنية> | !radio !queue !np !skip !stop | !move: come near you | 🎲 !game !guess !joke !dare !fortune !scores | Chat: Ramsei / رامسي 😎")
                    await self.say("Music search: !play <song or artist>, then reply 1-5 to choose. A YouTube link queues directly.")
                    if self.permissions.authorization_error(uid, verified, "!rooms") is None:
                        await self.say("Room controls: !join <room ID/link> | !leave <room ID/here> | !rooms | !mainroom. Main room stays connected.")
                    if self.permissions.authorization_error(uid, verified, "!addroomadmin") is None:
                        await self.say("Owner: !addroomadmin Username | !removeroomadmin Username | !roomadmins. Grant or revoke room-control access separately from regular admins.")
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
            await self.login_or_pause()
            delay = 5
            while True:
                try:
                    await self.update_account_presence()
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
                        self.chat_ready = False
                        self.last_appearance = 0
                        self.outfit_products = []
                        self.pending_chat_ops.clear()
                        for queue in (self.queue, f"inv:/chat/chat-{self.room}"):
                            operation = await self.packet("msg_c2g_subscribe", queues_with_results=[{"record": "subscription", "name": queue, "op_id": self.op + 1}])
                            if queue == self.queue:
                                self.room_subscription_op = operation
                        self.connected = True
                        LOG.info("Room transport authenticated for %s; waiting for subscription acknowledgement", self.room)
                        delay = 5
                        tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.presence())]
                        try:
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            self.connected = False
                            self.chat_ready = False
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                except AuthenticationRequired:
                    self.connected = False
                    LOG.warning("Saved login expired; trying password-only IMVU login")
                    await self.login_or_pause(force=True)
                except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, ValueError) as error:
                    detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                    LOG.warning("Room connection lost (%s); retrying in %s seconds", detail, delay)
                finally:
                    self.connected = False
                while not self.commands.empty():
                    self.commands.get_nowait()
                    self.commands.task_done()
                self.song_choices.clear()
                while not self.social.inbox.empty():
                    self.social.inbox.get_nowait()
                    self.social.inbox.task_done()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    from room_control import RoomManager
    manager = RoomManager()
    app = web.Application()
    app.router.add_get("/health", manager.health)
    app.router.add_get("/stream.mp3", manager.stream)
    app.router.add_get("/rooms/{room}/stream.mp3", manager.stream)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "3000"))).start()
    if not os.getenv("DATABASE_URL", "").strip():
        LOG.info("File state enabled; keep permissions and login paths on persistent storage (/app/data/ramsei).")
    LOG.info("Radio endpoint /stream.mp3; diagnostic status /health")
    terminal = None
    control = None
    try:
        await manager.start()
        if os.getenv("TERMINAL_CONTROL", "0") == "1":
            from local_control import LocalControl
            control = LocalControl(manager.command)
            await control.start()
            terminal = asyncio.create_task(manager.terminal())
        await asyncio.Event().wait()
    finally:
        if control:
            await control.close()
        if terminal:
            terminal.cancel()
            await asyncio.gather(terminal, return_exceptions=True)
        await manager.close()
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
