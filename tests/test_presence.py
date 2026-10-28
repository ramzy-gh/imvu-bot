import asyncio
import os
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bot import API, AuthenticationRequired, Bot, IMVURequestError, Radio


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_heartbeat_does_not_resend_unchanged_seat_or_outfit(self):
        bot = Bot(Radio())
        bot.user_id = "330286535"
        bot.packet = AsyncMock()
        bot.api = AsyncMock()
        bot.participants = AsyncMock()
        bot.announce_seat = AsyncMock()
        bot.announce_appearance = AsyncMock()
        bot.last_account_presence = time.monotonic()
        with patch("bot.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await bot.presence()
        bot.packet.assert_awaited_once_with("msg_c2g_ping")
        bot.participants.assert_awaited_once_with(greet=True)
        bot.announce_seat.assert_not_awaited()
        bot.announce_appearance.assert_not_awaited()

    async def test_transient_maintenance_keeps_connection_but_membership_loss_propagates(self):
        bot = Bot(Radio())
        bot.connected = bot.chat_ready = True
        for error in (IMVURequestError(503, "/chat/test"), IMVURequestError(429, "/chat/test"),
                      ConnectionError("temporary"), TimeoutError()):
            self.assertFalse(await bot.room_maintenance(AsyncMock(side_effect=error)()))
            self.assertTrue(bot.connected)
            self.assertTrue(bot.chat_ready)
        for error in (AuthenticationRequired("expired"), IMVURequestError(404, "/chat/test"),
                      IMVURequestError(403, "/chat/test")):
            with self.assertRaises(type(error)):
                await bot.room_maintenance(AsyncMock(side_effect=error)())
        self.assertTrue(await bot.room_maintenance(AsyncMock()()))

    async def test_appearance_uses_one_outfit_command_and_retries_unsent_announcement(self):
        bot = Bot(Radio())
        bot.connected = bot.chat_ready = True
        bot.user_id = "330286535"
        bot.appearance_pending = True
        bot.say = AsyncMock(return_value=False)
        bot.announce_seat = AsyncMock()
        with patch("bot.load_named_outfit", AsyncMock(return_value={"products": ["1", "2"]})):
            await bot.announce_appearance()
            self.assertTrue(bot.appearance_pending)
            bot.say.return_value = True
            await bot.announce_appearance()
        self.assertFalse(bot.appearance_pending)
        self.assertEqual(bot.say.await_args.args, ("*putOnOutfit 1 2",))
        self.assertFalse(any(call.args[0].startswith("*use") for call in bot.say.await_args_list))
        bot.say.reset_mock()
        await bot.announce_appearance()
        bot.say.assert_not_awaited()  # Do not keep reloading an unchanged outfit.

    async def test_own_account_presence_and_noncritical_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "DATABASE_URL": "", "PERMISSIONS_PATH": str(Path(folder) / "permissions.json")}):
            bot = Bot(Radio())
            bot.user_id = "330286535"
            bot.api = AsyncMock(return_value={"denormalized": {
                API + "/presence/presence-330286535": {"data": {"online": True}}}})
            await bot.update_account_presence()
            bot.api.assert_awaited_once_with("POST", "/presence/presence-330286535", json={"online": True})
            self.assertTrue(bot.account_presence["online"])
            bot.api.side_effect = RuntimeError("HTTP 503")
            await bot.update_account_presence()
            self.assertEqual(bot.account_presence["state"], "update_failed")
            bot.api.side_effect = AuthenticationRequired("expired")
            with self.assertRaises(AuthenticationRequired):
                await bot.update_account_presence()
