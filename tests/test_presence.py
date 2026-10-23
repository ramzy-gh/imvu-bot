import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bot import API, AuthenticationRequired, Bot, Radio


class PresenceTests(unittest.IsolatedAsyncioTestCase):
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
