"""Authenticated loopback control for shells separate from the bot's stdin."""
import asyncio
import contextlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile

LOG = logging.getLogger("ramsei.control")


def control_path():
    return Path(os.getenv("CONTROL_STATE_PATH", str(Path(tempfile.gettempdir()) / "ramsei-control.json")))


class LocalControl:
    def __init__(self, command, path=None):
        self.command = command
        self.path = Path(path) if path is not None else control_path()
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.lock = asyncio.Lock()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=2048)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp = tempfile.mkstemp(prefix=".ramsei-control-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"port": self.server.sockets[0].getsockname()[1], "token": self.token}, stream)
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            self.server.close()
            await self.server.wait_closed()
            raise
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
        LOG.info("Local terminal control ready: python botctl.py rooms (same container)")

    async def handle(self, reader, writer):
        try:
            data = json.loads(await asyncio.wait_for(reader.readline(), 3))
            supplied = data.get("token") if isinstance(data, dict) else None
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self.token):
                result = {"ok": False, "message": "Unauthorized local control request"}
            elif not isinstance(data.get("command"), str) or len(data["command"]) > 100:
                result = {"ok": False, "message": "Invalid control command"}
            else:
                async with self.lock:
                    try:
                        result = {"ok": True, "message": await self.command(data["command"])}
                    except ValueError as error:
                        result = {"ok": False, "message": str(error)}
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (ValueError, TypeError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        # Do not remove a replacement control file owned by another process.
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("token") == self.token:
                self.path.unlink()


async def send_command(command, path=None):
    path = Path(path) if path is not None else control_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(data["port"])), 3)
    try:
        writer.write(json.dumps({"token": data["token"], "command": command}).encode() + b"\n")
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 20))
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
