import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import API, Bot, Radio


class WelcomeTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, room="123-1"):
        bot = Bot(Radio(), room=room, permissions=SimpleNamespace(allowed=lambda uid: True))
        bot.user_id = "9"
        bot.connected = bot.chat_ready = True
        bot.say = AsyncMock(return_value=True)
        bot.occupants = {"9"}
        bot.fail_profile = False
        async def api(method, path):
            if path.endswith("/participants"):
                return {"denormalized": {API + path + "/user-" + uid: {"data": {}} for uid in bot.occupants}}
            if bot.fail_profile:
                raise ConnectionError("temporary")
            return {"denormalized": {API + path: {"data": {"username": "AccountName", "display_name": "Nickname"}}}}
        bot.api = AsyncMock(side_effect=api)
        return bot

    async def test_receive_membership_refresh_does_not_steal_welcome(self):
        bot = self.bot()
        await bot.participants()
        self.assertFalse(bot.appearance_pending)
        bot.occupants.add("1")
        await bot.participants(greet=False)
        self.assertTrue(bot.appearance_pending)
        await bot.participants(greet=True)
        await bot.participants(greet=True)
        bot.say.assert_awaited_once()
        self.assertIn("@AccountName", bot.say.await_args.args[0])
        self.assertNotIn("Nickname", bot.say.await_args.args[0])

    async def test_same_visitor_gets_separate_welcome_in_each_room(self):
        first, second = self.bot(), self.bot("123-2")
        await asyncio.gather(first.participants(), second.participants())
        first.occupants.add("1")
        second.occupants.add("1")
        await asyncio.gather(first.participants(greet=True), second.participants(greet=True))
        first.say.assert_awaited_once()
        second.say.assert_awaited_once()
        self.assertIsNot(first.greeted, second.greeted)
        self.assertIsNot(first.pending_welcomes, second.pending_welcomes)

    async def test_profile_failure_and_unsent_chat_retry_without_losing_member(self):
        bot = self.bot()
        await bot.participants()
        bot.occupants.add("1")
        bot.fail_profile = True
        await bot.participants(greet=True)
        self.assertIn("1", bot.members)
        self.assertIn("1", bot.pending_welcomes)
        self.assertNotIn("1", bot.greeted)
        bot.fail_profile = False
        bot.say.return_value = False
        await bot.participants(greet=True)
        self.assertIn("1", bot.pending_welcomes)
        bot.say.return_value = True
        await bot.participants(greet=True)
        self.assertFalse(bot.pending_welcomes)
        self.assertIn("1", bot.greeted)

    async def test_no_restart_spam_departed_or_blocked_welcomes(self):
        bot = self.bot()
        bot.occupants.add("old")
        await bot.participants(greet=True)
        bot.say.assert_not_awaited()
        bot.occupants.add("new")
        await bot.participants()
        bot.occupants.remove("new")
        await bot.participants(greet=True)
        bot.say.assert_not_awaited()
        bot.permissions.allowed = lambda uid: False
        bot.occupants.add("blocked")
        await bot.participants(greet=True)
        bot.say.assert_not_awaited()

    async def test_concurrent_flush_and_rejoin_do_not_duplicate(self):
        bot = self.bot()
        await bot.participants()
        bot.occupants.add("1")
        await bot.participants()
        await asyncio.gather(bot.send_welcomes(), bot.send_welcomes())
        bot.occupants.remove("1")
        await bot.participants()
        bot.occupants.add("1")
        await bot.participants(greet=True)
        bot.say.assert_awaited_once()
        bot.greeted["1"] = time.monotonic() - 301
        bot.occupants.remove("1")
        await bot.participants()
        bot.occupants.add("1")
        await bot.participants(greet=True)
        self.assertEqual(bot.say.await_count, 2)
