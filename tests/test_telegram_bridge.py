import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from bot import Bot, Radio, encode
from telegram_bridge import TelegramBridge, TelegramError


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        path = Path(self.folder.name)
        self.env = patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:" + "A" * 35,
            "TELEGRAM_OWNER_ID": "111111", "TELEGRAM_STATE_PATH": str(path / "telegram.json"), "DATABASE_URL": ""})
        self.env.start()
        self.first = SimpleNamespace(connected=True, chat_ready=True, say=AsyncMock(return_value=True), session=None)
        self.second = SimpleNamespace(connected=True, chat_ready=True, say=AsyncMock(return_value=True), session=None)
        self.manager = SimpleNamespace(permissions=SimpleNamespace(path=path / "permissions.json"),
                                      rooms={"123-1": (self.first, None), "123-2": (self.second, None)})
        self.bridge = TelegramBridge(self.manager)
        self.bridge.notify = AsyncMock()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def message(self, text, reply=None):
        result = {"from": {"id": 111111}, "chat": {"id": 111111, "type": "private"}, "text": text}
        if reply is not None:
            result["reply_to_message"] = {"message_id": reply, "from": {"id": 123456}}
        return result

    async def test_reply_routes_only_to_original_room(self):
        self.bridge.routes["10"] = "123-2"
        await self.bridge.handle_message(self.message("hello everyone", 10))
        self.second.say.assert_awaited_once_with("Ramzy (Telegram): hello everyone")
        self.first.say.assert_not_awaited()

    async def test_explicit_room_and_no_implicit_default_room(self):
        await self.bridge.handle_message(self.message("hello"))
        self.first.say.assert_not_awaited()
        self.second.say.assert_not_awaited()
        await self.bridge.handle_message(self.message("/say 123-1 hello"))
        self.first.say.assert_awaited_once_with("Ramzy (Telegram): hello")
        await self.bridge.handle_message(self.message("/say 123-3 hello"))
        self.assertIn("Nothing was sent", self.bridge.notify.await_args.args[0])

    async def test_other_accounts_groups_bots_and_forged_quotes_cannot_send(self):
        self.bridge.routes["10"] = "123-1"
        for variant in ["sender", "chat", "group", "bot", "quote"]:
            msg = self.message("hello", 10)
            if variant == "sender": msg["from"]["id"] = 222222
            if variant == "chat": msg["chat"]["id"] = 222222
            if variant == "group": msg["chat"]["type"] = "group"
            if variant == "bot": msg["from"]["is_bot"] = True
            if variant == "quote": msg["reply_to_message"]["from"]["id"] = 777777
            await self.bridge.handle_message(msg)
        self.first.say.assert_not_awaited()

    async def test_no_action_commands_truncation_or_false_success(self):
        for text in ["/say 123-1 *msg SeatAssignment 3 1 2 0", "/say 123-1 " + "x" * 431]:
            await self.bridge.handle_message(self.message(text))
        self.first.say.assert_not_awaited()
        self.first.say.return_value = False
        await self.bridge.handle_message(self.message("/say 123-1 hello"))
        self.assertIn("Could not send", self.bridge.notify.await_args.args[0])

    async def test_outbox_reply_routes_and_cursor_survive_restart(self):
        self.bridge.record("123-1", "1", "hello", "AccountName")
        self.bridge.routes["10"] = "123-1"
        self.bridge.offset = 20
        await self.bridge.persist()
        restarted = TelegramBridge(self.manager)
        with patch.object(restarted, "run", AsyncMock()):
            await restarted.start()
            await restarted.close()
        self.assertEqual(restarted.routes["10"], "123-1")
        self.assertEqual(restarted.offset, 20)
        self.assertEqual(restarted.outbox[0]["text"], "hello")
        self.assertNotIn(self.bridge.token, Path(self.bridge.store.path).read_text())

    async def test_batches_keep_rooms_separate_and_persist_reply_mapping(self):
        self.bridge.record("123-1", "1", "one", "First")
        self.bridge.record("123-2", "2", "other room", "Second")
        self.bridge.record("123-1", "3", "two", "Third")
        self.bridge.call = AsyncMock(side_effect=[{"message_id": 10}, {"message_id": 11}])
        worker = asyncio.create_task(self.bridge.deliver())
        self.bridge.task = worker
        try:
            async with asyncio.timeout(6):
                while len(self.bridge.routes) < 2:
                    await asyncio.sleep(.01)
        finally:
            await self.bridge.close()
        first = self.bridge.call.await_args_list[0].kwargs["text"]
        second = self.bridge.call.await_args_list[1].kwargs["text"]
        self.assertIn("@First: one", first)
        self.assertIn("@Third: two", first)
        self.assertNotIn("other room", first)
        self.assertIn("@Second: other room", second)
        self.assertEqual(dict(self.bridge.routes), {"10": "123-1", "11": "123-2"})
        self.assertFalse(self.bridge.outbox)
        saved = json.loads(Path(self.bridge.store.path).read_text())
        self.assertEqual(saved["routes"], {"10": "123-1", "11": "123-2"})

    async def test_cursor_is_saved_before_external_reply_and_duplicate_is_ignored(self):
        done = asyncio.Event()
        async def say(text):
            saved = json.loads(Path(self.bridge.store.path).read_text())
            self.assertEqual(saved["offset"], 6)
            done.set()
            return True
        self.first.say.side_effect = say
        update = {"update_id": 5, "message": self.message("/say 123-1 hi")}
        replies = [[update, update]]
        async def call(method, **kwargs):
            if replies:
                return replies.pop()
            await asyncio.Event().wait()
        self.bridge.call = call
        worker = asyncio.create_task(self.bridge.poll())
        try:
            await asyncio.wait_for(done.wait(), 2)
            await asyncio.sleep(.05)
            self.first.say.assert_awaited_once()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_private_state_failure_disables_only_bridge_and_owner_mismatch_fails_closed(self):
        with patch.object(self.bridge.store, "load", side_effect=OSError("unavailable")):
            await self.bridge.start()
        self.assertFalse(self.bridge.enabled)
        self.assertTrue(self.first.connected)
        other = TelegramBridge(self.manager)
        with patch.object(other.store, "load", return_value={"owner": 222222, "bot": 123456}):
            await other.start()
        self.assertFalse(other.enabled)

    async def test_network_errors_never_expose_token(self):
        self.bridge.session = SimpleNamespace(post=Mock(side_effect=aiohttp.ClientError(self.bridge.token)))
        with self.assertRaises(TelegramError) as error:
            await self.bridge.call("getMe")
        self.assertNotIn(self.bridge.token, str(error.exception))

    async def test_records_exclude_protocol_actions_and_are_bounded(self):
        self.bridge.record("123-1", "1", "*msg SeatAssignment 3 1 2 0")
        self.assertFalse(self.bridge.outbox)
        for _ in range(501):
            self.bridge.record("123-1", "1", "message")
        self.assertEqual(len(self.bridge.outbox), 500)
        self.assertEqual(self.bridge.dropped, 1)

    async def test_public_chat_is_relayed_even_when_sender_is_blocked_from_features(self):
        bot = Bot(Radio(), room="123-1", permissions=SimpleNamespace(allowed=lambda uid: False),
                  room_manager=SimpleNamespace(telegram=self.bridge))
        bot.user_id, bot.queue, bot.members, bot.chat_ready = "9", "q", {"1"}, True
        packet = {"record": "msg_g2c_send_message", "queue": "q", "mount": "messages", "user_id": "1",
                  "message": encode({"userId": "1", "message": "hello"})}
        class Socket:
            async def __aiter__(self):
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(packet))
        bot.ws = Socket()
        await bot.receive()
        self.assertEqual(self.bridge.outbox[0]["text"], "hello")
        self.assertTrue(bot.commands.empty())
