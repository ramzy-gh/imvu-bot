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
        self.second.say.assert_awaited_once_with("hello everyone")
        self.first.say.assert_not_awaited()

    async def test_explicit_room_and_no_implicit_default_room(self):
        await self.bridge.handle_message(self.message("hello"))
        self.first.say.assert_not_awaited()
        self.second.say.assert_not_awaited()
        await self.bridge.handle_message(self.message("/say 123-1 hello"))
        self.first.say.assert_awaited_once_with("hello")
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

    async def test_owner_grants_persistent_read_only_viewer_and_lists_them(self):
        async def call(method, **kwargs):
            if method == "getChat":
                return {"id": 222222, "type": "private", "username": "Friend"}
            return {"message_id": 1}
        self.bridge.call = AsyncMock(side_effect=call)
        await self.bridge.handle_message(self.message("/addviewer 222222"))
        self.assertEqual(self.bridge.viewers["222222"]["label"], "@Friend")
        await self.bridge.handle_message(self.message("/viewers"))
        self.assertIn("222222 — @Friend", self.bridge.notify.await_args.args[0])
        restarted = TelegramBridge(self.manager)
        with patch.object(restarted, "run", AsyncMock()):
            await restarted.start()
            await restarted.close()
        self.assertEqual(restarted.viewers, self.bridge.viewers)

    async def test_viewer_cannot_reply_delegate_revoke_or_list_access(self):
        self.bridge.viewers["222222"] = {"since": 1, "generation": "a" * 24, "label": "@Friend"}
        self.bridge.routes["10"] = "123-1"
        self.bridge.call = AsyncMock(return_value={"message_id": 1})
        for text in ["/addviewer 333333", "/removeviewer 222222", "/viewers", "/rooms", "/say 123-1 hi", "hi"]:
            message = self.message(text, 10)
            message["from"]["id"] = message["chat"]["id"] = 222222
            await self.bridge.handle_message(message)
        self.first.say.assert_not_awaited()
        self.second.say.assert_not_awaited()
        self.assertEqual(list(self.bridge.viewers), ["222222"])
        self.bridge.notify.assert_not_awaited()
        self.assertTrue(all(c.kwargs["chat_id"] == 222222 for c in self.bridge.call.await_args_list))

    async def test_unapproved_start_only_returns_id_and_requests_owner_approval_once(self):
        self.bridge.call = AsyncMock(return_value={"message_id": 1})
        msg = self.message("/start")
        msg["from"] = {"id": 222222, "username": "Friend"}
        msg["chat"]["id"] = 222222
        await self.bridge.handle_message(msg)
        await self.bridge.handle_message(msg)
        self.assertFalse(self.bridge.viewers)
        self.bridge.notify.assert_awaited_once()
        self.assertIn("/addviewer 222222", self.bridge.notify.await_args.args[0])
        self.assertIn("No room logs", self.bridge.call.await_args.kwargs["text"])

    async def test_viewers_receive_only_logs_after_grant_without_owner_reply_routes(self):
        self.bridge.viewers["222222"] = {"since": 2, "generation": "a" * 24, "label": "@Friend"}
        delivered = asyncio.Event()
        async def call(method, **kwargs):
            self.assertEqual(kwargs["chat_id"], 222222)
            self.assertIn("new log", kwargs["text"])
            self.assertNotIn("old log", kwargs["text"])
            delivered.set()
            return {"message_id": 99}
        self.bridge.call = AsyncMock(side_effect=call)
        self.bridge.fanout("123-1", [(1, "old log"), (2, "new log")])
        try:
            await asyncio.wait_for(delivered.wait(), 2)
            self.assertFalse(self.bridge.routes)
        finally:
            await self.bridge.close()

    async def test_revoke_waits_for_inflight_send_then_clears_queue_and_stays_revoked(self):
        self.bridge.viewers["222222"] = {"since": 1, "generation": "a" * 24, "label": "@Friend"}
        started, release = asyncio.Event(), asyncio.Event()
        async def call(method, **kwargs):
            started.set()
            await release.wait()
            return {"message_id": 1}
        self.bridge.call = AsyncMock(side_effect=call)
        self.bridge.fanout("123-1", [(1, "first log")])
        await asyncio.wait_for(started.wait(), 2)
        self.bridge.fanout("123-1", [(2, "queued log")])
        remove = asyncio.create_task(self.bridge.handle_message(self.message("/removeviewer 222222")))
        await asyncio.sleep(.03)
        self.assertFalse(remove.done())
        release.set()
        await asyncio.wait_for(remove, 2)
        self.assertNotIn("222222", self.bridge.viewers)
        self.assertNotIn("222222", self.bridge.viewer_queues)
        self.bridge.fanout("123-1", [(3, "after removal")])
        await asyncio.sleep(.03)
        self.bridge.call.assert_awaited_once()
        restarted = TelegramBridge(self.manager)
        with patch.object(restarted, "run", AsyncMock()):
            await restarted.start()
            await restarted.close()
        self.assertFalse(restarted.viewers)

    async def test_viewer_permission_save_failures_roll_back(self):
        self.bridge.call = AsyncMock(return_value={"id": 222222, "type": "private", "username": "Friend"})
        with patch.object(self.bridge.store, "save", side_effect=OSError("unavailable")):
            await self.bridge.handle_message(self.message("/addviewer 222222"))
        self.assertFalse(self.bridge.viewers)
        original = {"since": 1, "generation": "a" * 24, "label": "@Friend"}
        self.bridge.viewers["222222"] = original
        with patch.object(self.bridge.store, "save", side_effect=OSError("unavailable")):
            await self.bridge.handle_message(self.message("/removeviewer 222222"))
        self.assertEqual(self.bridge.viewers["222222"], original)

    async def test_invalid_ids_group_destinations_and_owner_removal_rejected(self):
        self.bridge.call = AsyncMock(return_value={"id": 222222, "type": "group"})
        for text in ["/addviewer -222222", "/addviewer @Friend", "/addviewer 222222", "/removeviewer 111111"]:
            await self.bridge.handle_message(self.message(text))
        self.assertFalse(self.bridge.viewers)
        self.assertIn("permanent", self.bridge.notify.await_args.args[0])
