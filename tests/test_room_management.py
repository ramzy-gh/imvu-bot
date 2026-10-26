import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from permissions import OWNER_ID, normalize_management_message
from room_control import RoomManager, room_id


class RoomManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATABASE_URL": "", "ADMIN_IDS": "", "IMVU_ROOM_IDS": "",
            "IMVU_ROOM_ID": "330286535-177", "PERMISSIONS_PATH": str(Path(self.temp.name) / "permissions.json"),
            "ROOMS_PATH": str(Path(self.temp.name) / "rooms.json")})
        self.env.start()
        self.manager = RoomManager()
        async def worker(bot):
            await bot.command_worker()
        self.mock_serve = patch.object(self.manager, "serve", worker)
        self.mock_serve.start()

    async def asyncTearDown(self):
        await self.manager.close()
        self.mock_serve.stop()
        self.env.stop()
        self.temp.cleanup()

    async def source(self, room="330286535-177"):
        await self.manager.join(room)
        bot, _ = self.manager.rooms[room]
        bot.say = AsyncMock()
        bot.connected = bot.chat_ready = True
        bot.members = {OWNER_ID, "123"}
        return bot

    async def test_owner_joins_from_one_room_and_leaves_it_from_another(self):
        main = await self.source()
        await self.manager.chat_command(main, OWNER_ID, True, "!join",
            "https://www.imvu.com/next/chat/room-47377071-5395/")
        second = await self.source("47377071-5395")
        await self.manager.chat_command(second, OWNER_ID, True, "!join", "123-1")
        third = await self.source("123-1")
        await self.manager.chat_command(third, OWNER_ID, True, "!leave", "47377071-5395")
        async with asyncio.timeout(2):
            while "47377071-5395" in self.manager.rooms:
                await asyncio.sleep(0.01)
        self.assertIn(main.room, self.manager.rooms)
        self.assertIn(third.room, self.manager.rooms)
        saved = json.loads(Path(os.environ["ROOMS_PATH"]).read_text())
        self.assertNotIn("47377071-5395", saved["rooms"])

    async def test_only_verified_present_owner_can_control_rooms(self):
        source = await self.source()
        self.manager.permissions.admins.add("123")
        for uid, verified in (("123", True), (OWNER_ID, False)):
            await self.manager.chat_command(source, uid, verified, "!join", "123-1")
        source.members.remove(OWNER_ID)
        await self.manager.chat_command(source, OWNER_ID, True, "!join", "123-1")
        self.assertNotIn("123-1", self.manager.rooms)
        self.assertNotIn("123-1", self.manager.desired_rooms)

    async def test_main_room_is_protected_from_chat_and_terminal(self):
        source = await self.source()
        await self.manager.chat_command(source, OWNER_ID, True, "!leave", "here")
        with self.assertRaisesRegex(ValueError, "protected"):
            await self.manager.command("leave 330286535-177")
        self.assertIn(source.room, self.manager.rooms)
        self.assertIn(source.room, self.manager.desired_rooms)
        source.say.assert_called_with("Main room 330286535-177 is protected and always stays connected.")

    async def test_delegated_access_works_across_rooms_and_revocation_is_immediate(self):
        main = await self.source()
        self.manager.permissions.manage(OWNER_ID, True, "!addroomadmin", "123", username="Helper")
        await self.manager.chat_command(main, "123", True, "!join", "123-1")
        extra = await self.source("123-1")
        await self.manager.chat_command(extra, "123", True, "!join", "123-2")
        self.assertIn("123-2", self.manager.rooms)
        await self.manager.chat_command(extra, "123", True, "!leave", main.room)
        self.assertIn(main.room, self.manager.rooms)
        self.assertIn("protected", extra.say.await_args.args[0])
        await self.manager.chat_command(main, "123", True, "!leave", extra.room)
        async with asyncio.timeout(2):
            while extra.room in self.manager.rooms:
                await asyncio.sleep(.01)
        self.manager.permissions.manage(OWNER_ID, True, "!removeroomadmin", "123")
        await self.manager.chat_command(main, "123", True, "!join", "123-3")
        self.assertNotIn("123-3", self.manager.rooms)
        self.assertIn("access", main.say.await_args.args[0])

    async def test_leave_current_extra_room_does_not_deadlock_worker(self):
        main = await self.source()
        source = await self.source("123-1")
        _, worker = self.manager.rooms[source.room]
        source.commands.put_nowait((OWNER_ID, "!leave here", True))
        async with asyncio.timeout(2):
            while source.room in self.manager.rooms:
                await asyncio.sleep(0.01)
        self.assertTrue(worker.done())
        self.assertIn(main.room, self.manager.rooms)

    async def test_rooms_survive_shutdown_and_restart(self):
        await self.manager.start()
        await self.manager.join("47377071-5395")
        await self.manager.close()
        restarted = RoomManager()
        async def idle(bot):
            await asyncio.Event().wait()
        with patch.object(restarted, "serve", idle):
            try:
                await restarted.start()
                self.assertEqual(set(restarted.rooms), {"330286535-177", "47377071-5395"})
            finally:
                await restarted.close()

    async def test_save_failure_does_not_apply_join_or_leave(self):
        await self.source()
        await self.source("123-1")
        before = self.manager.desired_rooms.copy()
        with patch.object(self.manager.room_store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(ValueError):
                await self.manager.join("123-2")
            with self.assertRaises(ValueError):
                await self.manager.leave("123-1")
        self.assertEqual(before, self.manager.desired_rooms)
        self.assertIn("123-1", self.manager.rooms)
        self.assertNotIn("123-2", self.manager.rooms)

    async def test_duplicate_concurrent_join_starts_one_worker(self):
        await asyncio.gather(*(self.manager.join("123-1") for _ in range(5)))
        self.assertEqual(list(self.manager.rooms), ["123-1"])

    async def test_room_limit_includes_main(self):
        for i in range(9):
            await self.manager.join(f"123-{i}")
        with self.assertRaisesRegex(ValueError, "Maximum 10"):
            await self.manager.join("123-99")

    async def test_links_are_parsed_without_arbitrary_urls(self):
        self.assertEqual(room_id("https://www.imvu.com/next/chat/room-47377071-5395/"), "47377071-5395")
        for value in ("https://evil.example/next/chat/room-123-1/", "https://www.imvu.com.evil.example/next/chat/room-123-1/", "123", "http://www.imvu.com/next/chat/room-123-1/"):
            with self.assertRaises(ValueError):
                room_id(value)
        self.assertEqual(normalize_management_message("/join 123-1"), "!join 123-1")
