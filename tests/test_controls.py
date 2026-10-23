import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot import Radio
from permissions import OWNER_ID, Permissions
from room_control import Authentication, RoomManager


class Controls(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATABASE_URL": "", "ADMIN_IDS": "",
            "PERMISSIONS_PATH": str(Path(self.temp.name) / "permissions.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    async def test_permissions_persist_and_protect_owner(self):
        p = Permissions()
        p.manage("123", True, "!addadmin", "456", "Friend")
        self.assertFalse(p.is_admin("456"))
        p.manage(OWNER_ID, False, "!addadmin", "456", "Friend")
        self.assertFalse(p.is_admin("456"))
        p.manage(OWNER_ID, True, "!addadmin", "456", "Friend")
        p.manage("456", True, "!block", OWNER_ID)
        self.assertTrue(p.allowed(OWNER_ID))
        p.manage("456", True, "!block", "789", "Visitor")
        self.assertFalse(Permissions().allowed("789"))
        p.manage(OWNER_ID, True, "!removeadmin", "456", "Friend")
        self.assertFalse(Permissions().is_admin("456"))
        p.manage(OWNER_ID, True, "!unblock", "789", "Visitor")
        self.assertTrue(Permissions().allowed("789"))

    async def test_room_isolation_and_leave(self):
        manager = RoomManager()
        async def fake_serve(bot):
            await asyncio.Event().wait()
        with patch.object(manager, "serve", fake_serve):
            await manager.join("123-1")
            await manager.join("123-2")
            a, ta = manager.rooms["123-1"]
            b, tb = manager.rooms["123-2"]
            self.assertIs(a.permissions, b.permissions)
            a.radio.queue.put_nowait({"title": "Only A"})
            self.assertTrue(b.radio.queue.empty())
            self.assertIsNot(a.social, b.social)
            await asyncio.sleep(0)
            await manager.command("leave 123-1")
            self.assertTrue(ta.done())
            self.assertFalse(tb.done())
            self.assertIn("123-2", await manager.command("rooms"))
            await manager.close()
        with self.assertRaises(ValueError):
            await manager.join("https://example.com")

    async def test_login_failure_not_retried_for_every_room(self):
        auth = Authentication()
        class FakeBot:
            _login = AsyncMock(side_effect=RuntimeError("paused"))
        bot = FakeBot()
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await auth.login(bot)
        bot._login.assert_awaited_once()

    async def test_audio_survives_chat_failure(self):
        radio = Radio()
        radio.queue.put_nowait({"title": "Test", "url": "https://example.com/audio"})
        delivered = asyncio.Event()
        class Output:
            async def read(self, size):
                await asyncio.sleep(0)
                return b"mp3"
        class Process:
            stdout = Output()
            returncode = None
            def terminate(self):
                self.returncode = 0
            async def wait(self):
                return self.returncode
        async def broadcast(chunk):
            delivered.set()
        with patch("bot.asyncio.create_subprocess_exec", AsyncMock(return_value=Process())), patch.object(radio, "broadcast", broadcast):
            task = asyncio.create_task(radio.run(AsyncMock(side_effect=ConnectionError())))
            await asyncio.wait_for(delivered.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_slow_listener_and_shutdown(self):
        radio = Radio()
        listener = asyncio.Queue(maxsize=1)
        listener.put_nowait(b"old")
        radio.listeners.add(listener)
        await radio.broadcast(b"new")
        self.assertIsNone(listener.get_nowait())
        self.assertNotIn(listener, radio.listeners)
        await radio.broadcast(None)
