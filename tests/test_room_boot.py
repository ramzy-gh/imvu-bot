import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from permissions import Permissions, OWNER_ID
from room_moderation import boot_user, native_moderators, API


class BootTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {'DATABASE_URL': '', 'ADMIN_IDS': '',
            'PERMISSIONS_PATH': str(Path(temp) / 'permissions.json'), 'IMVU_ROOM_ID': '123-1'}))
        self.p = Permissions('123-1')
        self.p.room_moderation['123-1'] = ({'111'}, set())
        self.bot = SimpleNamespace(room='123-1', user_id='222', connected=True, chat_ready=True,
            members={'111', '222', '555', '123', OWNER_ID}, permissions=self.p,
            allowed_in_room=lambda uid: self.p.allowed(uid, '123-1'), say=AsyncMock(return_value=True),
            participants=AsyncMock(), room_manager=None)
        self.mods = {'222'}
        async def api(method, path, **kwargs):
            if method == 'GET' and path.endswith('/moderators'):
                return {'denormalized': {API + path: {'data': {'items': [API + path + '/user-' + u for u in self.mods]}}}}
            if method == 'DELETE':
                return {}
            raise AssertionError((method, path))
        self.bot.api = AsyncMock(side_effect=api)
        self.resolve = self.enterContext(patch('room_moderation.resolve_username', AsyncMock(return_value=('555', 'Guest_Target'))))

    def assert_no_boot(self):
        self.assertFalse(any(c.args[0] == 'DELETE' for c in self.bot.api.await_args_list))
        self.assertFalse(any(c.args[0].startswith('*') for c in self.bot.say.await_args_list))

    async def test_room_admin_boots_exact_username_only_in_current_room(self):
        await boot_user(self.bot, '111', True, 'Target')
        self.resolve.assert_awaited_once_with(self.bot.api, 'Target')
        self.assertEqual(self.bot.api.await_args.args, ('DELETE', '/chat/chat-123-1/participants/user-555'))
        self.assertIn('*imvu:txnBoot 555', [c.args[0] for c in self.bot.say.await_args_list])

    async def test_unverified_absent_and_wrong_room_admins_cannot_boot(self):
        for uid, verified in [('111', False), ('999', True)]:
            await boot_user(self.bot, uid, verified, 'Target')
        self.p.room_moderation['456-1'] = ({'555'}, set())
        await boot_user(self.bot, '555', True, 'Target')
        self.resolve.assert_not_awaited()
        self.assert_no_boot()

    async def test_owner_bot_native_moderator_global_mod_and_any_bot_admin_are_protected(self):
        self.p.mods.add('666')
        self.p.room_moderation['456-1'] = ({'777'}, set())
        self.mods.add('888')
        for target in [OWNER_ID, '222', '123', '111', '666', '777', '888']:
            self.bot.members.add(target)
            self.resolve.return_value = (target, 'Target')
            await boot_user(self.bot, '111', True, 'Target')
        self.assert_no_boot()

    async def test_moderator_promotion_during_lookup_prevents_boot(self):
        async def resolve(*args):
            self.mods.add('555')
            return '555', 'Target'
        self.resolve.side_effect = resolve
        await boot_user(self.bot, '111', True, 'Target')
        self.assert_no_boot()

    async def test_admin_revocation_during_lookup_prevents_boot(self):
        async def resolve(*args):
            self.p.room_moderation['123-1'][0].discard('111')
            return '555', 'Target'
        self.resolve.side_effect = resolve
        await boot_user(self.bot, '111', True, 'Target')
        self.assert_no_boot()

    async def test_bot_without_native_moderator_rights_does_not_announce_or_delete(self):
        self.mods.clear()
        await boot_user(self.bot, '111', True, 'Target')
        self.assert_no_boot()
        self.assertIn('must be an IMVU room moderator', self.bot.say.await_args.args[0])

    async def test_incomplete_or_unavailable_native_roles_fail_closed(self):
        self.bot.api.side_effect = RuntimeError('unavailable')
        await boot_user(self.bot, '111', True, 'Target')
        self.assert_no_boot()
        self.bot.api.side_effect = None
        self.bot.api.return_value = {'denormalized': {}}
        await boot_user(self.bot, '111', True, 'Target')
        self.assert_no_boot()

    async def test_moderator_pagination_is_checked_and_never_follows_foreign_host(self):
        path = '/room/room-123-1/moderators'
        self.bot.api.side_effect = None
        self.bot.api.return_value = {'denormalized': {API + path: {'data': {'items': []},
            'relations': {'next': 'https://attacker.test/moderators'}}}}
        with self.assertRaises(ValueError):
            await native_moderators(self.bot)
        self.assertEqual(self.bot.api.await_count, 1)

    async def test_absent_target_is_not_booted(self):
        self.bot.members.discard('555')
        await boot_user(self.bot, '111', True, 'Target')
        self.assert_no_boot()

    async def test_native_scoped_references_are_accepted_only_for_the_current_room(self):
        self.assertEqual(await native_moderators(self.bot), {'222'})
        path = '/room/room-123-1/moderators'
        self.bot.api.side_effect = None
        self.bot.api.return_value = {'denormalized': {API + path: {'data': {'items': [
            API + '/room/room-456-1/moderators/user-555']}}}}
        with self.assertRaises(ValueError):
            await native_moderators(self.bot)
