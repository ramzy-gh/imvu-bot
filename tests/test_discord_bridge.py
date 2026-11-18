import asyncio
import copy
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from discord_bridge import DiscordBridge, DiscordError, LIMIT, size
from discord_community import DiscordCommunity, VIEW
from ramsei_web import Portal


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DATABASE_URL": "", "DISCORD_BOT_TOKEN": "not-a-real-bot-token",
            "DISCORD_OWNER_ID": "111111111", "DISCORD_GUILD_ID": "999999999",
            "DISCORD_STATE_PATH": str(Path(self.temp) / "discord.json"),
            "RAMSEI_WEB_DB": str(Path(self.temp) / "website.sqlite3"),
            "RAMSEI_WEB_OWNER_EMAIL": "owner@example.test", "RAMSEI_WEB_OWNER_PASSWORD": "testing owner password",
            "RAMZY_VCOIN_TEST": "0", "RAMZY_SMTP_HOST": "", "IMVU_ROOM_IDS": "",
        }))
        self.first = SimpleNamespace(connected=True, chat_ready=True, room="123-1", room_name="First",
                                     say=AsyncMock(return_value=True), commands=asyncio.Queue())
        self.second = SimpleNamespace(connected=True, chat_ready=True, room="123-2", room_name="Second",
                                      say=AsyncMock(return_value=True), commands=asyncio.Queue())
        self.manager = SimpleNamespace(default_room="1-0", rooms={"123-1": (self.first, None), "123-2": (self.second, None)},
            permissions=SimpleNamespace(path=Path(self.temp) / "permissions.json"),
            settings=SimpleNamespace(set=AsyncMock(), update=AsyncMock(),
                get=lambda r: {"quiet": False, "welcomes": True, "welcome": ""}),
            set_language=AsyncMock(return_value="en"))
        self.bridge = DiscordBridge(self.manager)
        self.bridge.bot_id, self.bridge.channel = "222222222", "333333333"
        self.bridge.send = AsyncMock(return_value={"id": "444444444"})

    def message(self, text, *, uid="111111111", channel="333333333", reference=None):
        message = {"id": "555555555", "author": {"id": uid}, "channel_id": channel, "content": text}
        if reference:
            message["message_reference"] = {"message_id": reference}
        return message

    async def test_reply_is_pinned_to_original_room(self):
        self.bridge.routes["444444444"] = "123-2"
        await self.bridge.handle(self.message("hello", reference="444444444"))
        self.second.say.assert_awaited_once_with("hello")
        self.first.say.assert_not_awaited()

    async def test_other_users_channels_and_guild_messages_cannot_control(self):
        for message in (self.message("!say 123-1 hi", uid="999999998"),
                        self.message("!say 123-1 hi", channel="999999997"),
                        {**self.message("!say 123-1 hi"), "guild_id": "999999999"},
                        {**self.message("!say 123-1 hi"), "author": {"id": "111111111", "bot": True}}):
            await self.bridge.handle(message)
        self.first.say.assert_not_awaited()
        self.bridge.send.assert_not_awaited()

    async def test_no_inferred_room_actions_or_public_command_injection(self):
        for text in ("hello", "!say 123-1 *msg SeatAssignment", "!say 123-1 !addmod attacker", "!say 123-3 hi"):
            await self.bridge.handle(self.message(text))
        self.first.say.assert_not_awaited()

    async def test_welcome_default_and_language_use_shared_room_settings(self):
        await self.bridge.handle(self.message("!welcome 123-2 default"))
        self.manager.settings.update.assert_awaited_once_with("123-2", {"welcome": "", "welcomes": True})
        await self.bridge.handle(self.message("!language 123-2 en"))
        self.manager.set_language.assert_awaited_once_with(self.second, "en")

    async def test_only_explicit_owner_command_uses_verified_imvu_owner(self):
        from permissions import OWNER_ID
        await self.bridge.handle(self.message("!imvu 123-2 !admins"))
        self.assertEqual(self.second.commands.get_nowait(), (OWNER_ID, "!admins", True))
        self.assertTrue(self.first.commands.empty())

    def test_batches_are_room_separated_and_fit_even_for_emoji(self):
        self.bridge.record("123-1", "1", "😀" * 600)
        self.bridge.record("123-2", "2", "other room")
        self.bridge.record("123-1", "3", "😀" * 600)
        room, text, ids = self.bridge.batch()
        self.assertEqual(room, "123-1")
        self.assertNotIn("other room", text)
        self.assertLessEqual(size(text), 1900)
        self.assertEqual(len(ids), 1)

    async def test_sends_disable_mentions_and_previews(self):
        self.bridge.api = AsyncMock(return_value={"id": "444444444"})
        await DiscordBridge.send(self.bridge, "@everyone https://example.test")
        payload = self.bridge.api.await_args.kwargs["json"]
        self.assertEqual(payload["allowed_mentions"]["parse"], [])
        self.assertEqual(payload["flags"], 4)

    async def test_queue_and_cursor_survive_restart_without_tokens(self):
        for i in range(700):
            self.bridge.record("123-1", "1", str(i))
        self.bridge.cursor = "0"
        self.bridge.routes["444444444"] = "123-2"
        await self.bridge.persist()
        restored = DiscordBridge(self.manager)
        with patch.object(restored, "run", new=AsyncMock()):
            await restored.start()
            await restored.close()
        self.assertTrue(restored.enabled)
        self.assertEqual(len(restored.outbox), 700)
        self.assertEqual(restored.cursor, "0")
        self.assertEqual(restored.routes["444444444"], "123-2")
        self.assertNotIn(self.bridge.token, Path(self.bridge.store.path).read_text())

    async def test_owner_change_fails_closed(self):
        await self.bridge.persist()
        self.bridge.owner = "888888888"
        await self.bridge.start()
        self.assertFalse(self.bridge.enabled)
        self.assertEqual(self.bridge.state, "storage_error")

    async def test_telegram_backlog_imports_once_and_preserves_original(self):
        from state_store import StateStore
        path = str(Path(self.temp) / "telegram.json")
        source = StateStore("telegram-bridge", path)
        original = {"owner": 123456, "bot": 654321, "outbox": [{"id": 7, "room": "123-2",
            "uid": "1", "text": "old queued message", "at": 1700000000, "name": None}]}
        source.save(original)
        with patch.dict(os.environ, {"CHAT_RELAY": "discord", "TELEGRAM_STATE_PATH": path,
                "TELEGRAM_OWNER_ID": "123456", "TELEGRAM_BOT_TOKEN": "654321:fake"}):
            with patch.object(self.bridge, "run", new=AsyncMock()):
                await self.bridge.start()
                await self.bridge.close()
            self.assertEqual(len(self.bridge.outbox), 1)
            restarted = DiscordBridge(self.manager)
            with patch.object(restarted, "run", new=AsyncMock()):
                await restarted.start()
                await restarted.close()
            self.assertEqual(len(restarted.outbox), 1)
            self.assertTrue(restarted.telegram_imported)
        self.assertEqual(source.load(), original)

    async def test_checkpoint_saves_new_messages_without_network_workers(self):
        async def sleep(delay):
            if not self.bridge.outbox:
                self.bridge.record("123-1", "1", "offline startup message")
            else:
                raise asyncio.CancelledError()
        with patch("discord_bridge.asyncio.sleep", new=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.bridge.checkpoint()
        self.assertEqual(self.bridge.store.load()["outbox"][0]["text"], "offline startup message")

    async def test_messages_arriving_during_storage_load_are_not_lost(self):
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set()
            release.wait(2)
            return None
        self.bridge.record("123-1", "1", "before load")
        with patch.object(self.bridge.store, "load", side_effect=load), patch.object(self.bridge, "run", new=AsyncMock()):
            task = asyncio.create_task(self.bridge.start())
            await asyncio.to_thread(entered.wait, 2)
            self.bridge.record("123-2", "2", "during load")
            release.set()
            await task
            await self.bridge.close()
        self.assertEqual({e["text"] for e in self.bridge.outbox}, {"before load", "during load"})

    async def test_delivered_events_persist_routes_and_clear_outbox(self):
        self.bridge.record("123-1", "1", "keep routing")
        done = asyncio.Event()
        original = self.bridge.persist
        async def persist():
            await original()
            if self.bridge.routes:
                done.set()
        self.bridge.persist = persist
        task = asyncio.create_task(self.bridge.deliver())
        try:
            await asyncio.wait_for(done.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        saved = self.bridge.store.load()
        self.assertFalse(saved["outbox"])
        self.assertEqual(saved["routes"]["444444444"], "123-1")

    async def test_network_send_failure_keeps_pending_event(self):
        self.bridge.record("123-1", "1", "retain")
        self.bridge.send.side_effect = DiscordError()
        with self.assertRaises(DiscordError):
            await self.bridge.deliver()
        self.assertEqual(self.bridge.outbox[0]["text"], "retain")

    async def test_remove_room_does_not_clear_other_rooms(self):
        self.bridge.record("123-1", "1", "remove")
        self.bridge.record("123-2", "1", "keep")
        self.bridge.routes.update({"444444444": "123-1", "555555555": "123-2"})
        await self.bridge.remove_room("123-1")
        self.assertEqual(self.bridge.outbox[0]["room"], "123-2")
        self.assertEqual(dict(self.bridge.routes), {"555555555": "123-2"})

    async def test_community_uses_separate_private_room_channels(self):
        community = DiscordCommunity(self.bridge)
        community.categories["IMVU ROOM LOGS"] = "888888888"
        community.ensure_channel = AsyncMock(side_effect=[{"id": "666666666"}, {"id": "777777777"}])
        first = await community.room_channel("123-1")
        second = await community.room_channel("123-2")
        self.assertNotEqual(first, second)
        self.assertEqual(await community.room_channel("123-1"), first)
        self.assertEqual(community.ensure_channel.await_count, 2)
        self.assertTrue(all(c.kwargs["private"] for c in community.ensure_channel.await_args_list))
        overwrites = community.private_overwrites()
        self.assertEqual(overwrites[0]["id"], community.guild)
        self.assertTrue(int(overwrites[0]["deny"]) & VIEW)
        self.assertEqual({o["id"] for o in overwrites[1:]}, {self.bridge.owner, self.bridge.bot_id})

    async def test_non_owner_cannot_open_payment_confirmation(self):
        community = DiscordCommunity(self.bridge)
        community.callback = AsyncMock()
        community.modal = AsyncMock()
        await community.interact({"id": "555555555", "token": "test", "guild_id": community.guild,
            "member": {"user": {"id": "666666666"}}, "type": 3,
            "data": {"custom_id": "payment|order|event"}})
        community.modal.assert_not_awaited()
        self.assertEqual(community.callback.await_args.args[1], 4)

    async def test_owner_reply_modal_uses_exact_room_and_ephemeral_response(self):
        community = DiscordCommunity(self.bridge)
        self.bridge.community = community
        self.bridge.send = DiscordBridge.send.__get__(self.bridge)
        community.callback = AsyncMock()
        community.followup = AsyncMock()
        await community.interact({"id": "555555555", "token": "test", "guild_id": community.guild,
            "member": {"user": {"id": self.bridge.owner}}, "type": 5,
            "data": {"custom_id": "reply|123-2", "components": [{"components": [{"custom_id": "value", "value": "hello from Discord"}]}]}})
        self.second.say.assert_awaited_once_with("hello from Discord")
        self.first.say.assert_not_awaited()
        self.assertEqual(community.callback.await_args.args[2]["flags"], 64)
        community.followup.assert_awaited_once()

    async def test_rate_limit_delay_is_observed_without_exposing_token(self):
        response = SimpleNamespace(status=429, headers={"Retry-After": "65"},
                                   json=AsyncMock(return_value={"retry_after": 65}))
        cm = Mock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=False)
        self.bridge.session = SimpleNamespace(request=Mock(return_value=cm))
        with self.assertRaises(DiscordError) as caught:
            await self.bridge.api("GET", "/users/@me")
        self.assertEqual(caught.exception.delay, 65)
        self.assertNotIn(self.bridge.token, str(caught.exception))

    async def test_community_setup_reuses_channels_roles_and_commands(self):
        community = DiscordCommunity(self.bridge)
        community.seed = AsyncMock()
        channels, roles, commands = [], [], []
        next_id = 600000000
        created = []
        async def api(method, path, **kwargs):
            nonlocal next_id, commands
            if method == "GET":
                if path == "/guilds/" + community.guild:
                    return {"owner_id": self.bridge.owner}
                if path.endswith("/channels"): return copy.deepcopy(channels)
                if path.endswith("/roles"): return copy.deepcopy(roles)
                if path.endswith("/commands"): return copy.deepcopy(commands)
            if method == "POST" and (path.endswith("/channels") or path.endswith("/roles")):
                next_id += 1
                data = {"id": str(next_id), **kwargs["json"]}
                (channels if path.endswith("/channels") else roles).append(copy.deepcopy(data))
                created.append(path)
                return copy.deepcopy(data)
            if method == "PUT" and path.endswith("/commands"):
                commands = copy.deepcopy(kwargs["json"])
                return commands
            if method == "PUT": return {}
            if method == "PATCH":
                channel = next(c for c in channels if c["id"] == path.rsplit("/", 1)[1])
                channel.update(kwargs["json"])
                return copy.deepcopy(channel)
            raise AssertionError((method, path))
        community.api = api
        await community.setup()
        count = len(created)
        await community.setup()
        self.assertEqual(len(created), count)
        self.assertEqual(len(roles), 5)
        self.assertTrue(all(not int(r["permissions"]) & 8 for r in roles))
        self.assertTrue(all(c["default_member_permissions"] == "0" for c in commands))
        for name in ("IMVU ROOM LOGS", "PRIVATE CONTROL", "VCOIN ORDERS"):
            category = next(c for c in channels if c["name"] == name)
            self.assertTrue(int(category["permission_overwrites"][0]["deny"]) & VIEW)

    def billing(self):
        portal = Portal(self.manager)
        self.addCleanup(portal.db.close)
        portal.create_account("buyer@example.test", "test buyer password")
        portal.db.execute("UPDATE accounts SET email_verified = 1 WHERE email = 'buyer@example.test'")
        portal.db.execute("INSERT INTO rooms(id, customer, status) VALUES ('123-2', 'buyer@example.test', 'pending')")
        portal.db.execute("""INSERT INTO vcoin_orders(id, room, amount, reported_sender, report_event)
            VALUES ('0123456789abcdef', '123-2', 700, 'BuyerIMVU', 'fresh-event')""")
        self.manager.portal = portal
        return portal, DiscordCommunity(self.bridge)

    async def test_ticket_creation_never_auto_confirms_payment_and_is_idempotent(self):
        portal, community = self.billing()
        community.categories["VCOIN ORDERS"] = "888888888"
        community.ensure_channel = AsyncMock(return_value={"id": "777777777"})
        community.api = AsyncMock(return_value={"id": "666666666"})
        await community.sync_orders()
        await community.sync_orders()
        community.ensure_channel.assert_awaited_once()
        self.assertEqual(community.api.await_count, 1)
        self.assertIsNone(portal.one("SELECT transfer FROM vcoin_orders")["transfer"])
        sent = community.api.await_args.kwargs["json"]
        self.assertNotIn("buyer@example.test", str(sent))
        self.assertFalse(sent["components"][0]["components"][0]["disabled"])

    async def test_stale_report_and_duplicate_confirmation_cannot_extend_access_twice(self):
        portal, community = self.billing()
        with self.assertRaises(ValueError):
            await community.confirm_order("0123456789abcdef", "old-event", "wallet-123")
        with self.assertRaises(ValueError):
            await community.confirm_order("0123456789abcdef", "fresh-event", "bad")
        await community.confirm_order("0123456789abcdef", "fresh-event", "wallet-123")
        access = dict(portal.room_access("123-2"))
        with self.assertRaises(ValueError):
            await community.confirm_order("0123456789abcdef", "fresh-event", "wallet-456")
        self.assertEqual(dict(portal.room_access("123-2")), access)
        self.assertEqual(portal.one("SELECT transfer FROM vcoin_orders")["transfer"], "wallet-123")
        self.assertEqual(portal.one("SELECT status FROM rooms WHERE id = '123-2'")["status"], "approved")

    async def test_pending_unreported_order_has_no_enabled_confirm_action(self):
        portal, community = self.billing()
        portal.db.execute("UPDATE vcoin_orders SET reported_sender = NULL, report_event = NULL")
        community.categories["VCOIN ORDERS"] = "888888888"
        community.ensure_channel = AsyncMock(return_value={"id": "777777777"})
        community.api = AsyncMock(return_value={"id": "666666666"})
        await community.sync_orders()
        sent = community.api.await_args.kwargs["json"]
        self.assertTrue(sent["components"][0]["components"][0]["disabled"])
        with self.assertRaises(ValueError):
            await community.confirm_order("0123456789abcdef", "", "wallet-123")
