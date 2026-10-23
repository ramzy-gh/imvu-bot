import unittest
from unittest.mock import AsyncMock

from identity import UserLookupError, exact_user, resolve_username
from permissions import normalize_management_message


def profile(uid, username, display_name):
    return {"https://api.imvu.com/user/user-" + uid:
            {"data": {"username": username, "display_name": display_name}}}


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_classic_name_resolves_guest_account_not_nickname(self):
        data = {"denormalized": profile("178133315", "Guest_C4MU", "Night")}
        api = AsyncMock(return_value=data)
        self.assertEqual(await resolve_username(api, "@c4mu"), ("178133315", "Guest_C4MU"))
        api.assert_awaited_once_with("GET", "/user?username=c4mu")
        self.assertEqual(exact_user(data, "Guest_C4MU"), ("178133315", "Guest_C4MU"))
        with self.assertRaises(UserLookupError):
            exact_user(data, "Night")

    async def test_exact_account_wins_over_guest_alias(self):
        data = {"denormalized": {**profile("1", "Guest_C4MU", "Night"),
                                 **profile("2", "C4MU", "Other")}}
        self.assertEqual(exact_user(data, "C4MU"), ("2", "C4MU"))

    async def test_ambiguous_account_denied(self):
        data = {"denormalized": {**profile("1", "Guest_C4MU", "Night"),
                                 **profile("2", "Guest_C4MU", "Night")}}
        with self.assertRaises(UserLookupError):
            exact_user(data, "C4MU")

    async def test_management_slash_aliases(self):
        for command in ("addadmin", "removeadmin", "block", "unblock", "admins", "blocked"):
            self.assertEqual(normalize_management_message(" /" + command.upper() + " @C4MU "),
                             "!" + command + " @C4MU")
        self.assertEqual(normalize_management_message("/dance"), "/dance")
        self.assertEqual(normalize_management_message("hello /addadmin C4MU"), "hello /addadmin C4MU")
