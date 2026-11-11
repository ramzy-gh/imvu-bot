import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import Bot, Radio
from permissions import OWNER_ID, OWNER_COMMANDS, Permissions, ROOM_COMMANDS, normalize_management_message


class ModRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "permissions.json"
        self.env = patch.dict(os.environ, {"DATABASE_URL": "", "ADMIN_IDS": "",
            "PERMISSIONS_PATH": str(self.path), "IMVU_ROOM_ID": "123-1"})
        self.env.start()
        self.permissions = Permissions()
        self.permissions.manage(OWNER_ID, True, "!addmod", "11", "Moderator")

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def make_bot(self):
        manager = SimpleNamespace(desired_rooms={"123-1", "123-2"}, rooms={})
        bot = Bot(Radio(), room="123-1", permissions=self.permissions, room_manager=manager)
        bot.members = {OWNER_ID, "11", "22"}
        bot.say = AsyncMock()
        bot.api = AsyncMock(return_value={})
        return bot

    async def test_mod_grants_and_removes_scoped_admins_in_every_room(self):
        p = self.permissions
        for room in ("123-1", "123-2"):
            self.assertIsNone(p.authorization_error("11", True, "!addadmin", room))
            p.manage("11", True, "!addadmin", "22", "Helper", room=room)
            self.assertTrue(p.is_admin("22", room))
            self.assertIn("@Helper", p.manage("11", True, "!admins", room=room))
        p.manage("11", True, "!removeadmin", "22", room="123-2")
        loaded = Permissions()
        self.assertTrue(loaded.is_admin("22", "123-1"))
        self.assertFalse(loaded.is_admin("22", "123-2"))
        self.assertEqual(loaded.mods, {"11"})
        for command in ROOM_COMMANDS:
            self.assertIsNone(loaded.authorization_error("11", True, command, "123-2"))

    async def test_mods_cannot_modify_owner_privileges_or_other_mods(self):
        p = self.permissions
        for command in OWNER_COMMANDS | {"!language", "!addmod", "!removemod", "!addroomadmin", "!removeroomadmin"}:
            self.assertIsNotNone(p.authorization_error("11", True, command, "123-2"))
            p.manage("11", True, command, "33", "Other", room="123-2")
        for command in ("!removeadmin", "!removemod", "!block", "!ownerblock"):
            p.manage("11", True, command, OWNER_ID, room="123-1")
        self.assertEqual(p.mods, {"11"})
        self.assertNotIn(OWNER_ID, p.mods)
        self.assertTrue(p.allowed(OWNER_ID, "123-1"))

    async def test_local_admins_cannot_invite_promote_or_block_mods(self):
        p = self.permissions
        p.manage("11", True, "!addadmin", "22", "Helper", room="123-1")
        for command in ROOM_COMMANDS | {"!addadmin", "!removeadmin", "!addmod", "!removemod"}:
            self.assertIsNotNone(p.authorization_error("22", True, command, "123-1"))
        self.assertIsNone(p.authorization_error("22", True, "!block", "123-1"))
        self.assertIsNotNone(p.authorization_error("22", True, "!block", "123-2"))
        p.manage("22", True, "!block", "11", room="123-1")
        self.assertTrue(p.allowed("11", "123-1"))
        self.assertNotIn("11", p.moderation("123-1")[1])

    async def test_owner_blocks_and_owner_only_override_mods_everywhere(self):
        p = self.permissions
        p.manage(OWNER_ID, True, "!ownerblock", "11")
        for room in ("123-1", "123-2"):
            self.assertIsNotNone(p.authorization_error("11", True, "!addadmin", room))
            self.assertIsNotNone(p.authorization_error("11", True, "!join", room))
        p.manage(OWNER_ID, True, "!ownerunblock", "11")
        p.manage(OWNER_ID, True, "!owneronly", "on")
        self.assertIsNotNone(p.authorization_error("11", True, "!join", "123-2"))
        p.manage(OWNER_ID, True, "!owneronly", "off")
        self.assertIsNone(p.authorization_error("11", True, "!join", "123-2"))

    async def test_legacy_migration_preserves_room_roles_and_revoke_stays_revoked(self):
        self.path.write_text(json.dumps({"admins": [], "blocked": [], "room_admins": ["11"],
            "room_moderation": {"123-2": {"admins": ["22"], "blocked": ["33"]}},
            "usernames": {"11": "Moderator", "22": "Helper"}}))
        migrated = Permissions()
        self.assertEqual(migrated.mods, {"11"})
        self.assertTrue(migrated.is_admin("22", "123-2"))
        self.assertFalse(migrated.allowed("33", "123-2"))
        migrated.manage(OWNER_ID, True, "!removemod", "11")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["mods"], [])
        self.assertEqual(saved["room_admins"], [])
        saved["room_admins"] = ["11"]
        self.path.write_text(json.dumps(saved))
        self.assertFalse(Permissions().mods)

    async def test_failed_mod_save_and_failed_admin_save_roll_back(self):
        p = self.permissions
        with patch.object(p.store, "save", side_effect=OSError("storage unavailable")):
            p.manage(OWNER_ID, True, "!removemod", "11")
            p.manage(OWNER_ID, True, "!addmod", "33", "Other")
            p.manage("11", True, "!addadmin", "22", "Helper", room="123-2")
        self.assertEqual(p.mods, {"11"})
        self.assertFalse(p.is_admin("22", "123-2"))
        self.assertNotIn("22", p.names)
        self.assertNotIn("33", p.names)

    async def test_remote_room_chat_commands_update_only_the_selected_room(self):
        bot = self.make_bot()
        with patch("bot.resolve_username", new=AsyncMock(return_value=("22", "Helper"))):
            await bot.management_command("11", True, "!addadmin", "Helper 123-2")
            self.assertTrue(self.permissions.is_admin("22", "123-2"))
            self.assertFalse(self.permissions.is_admin("22", "123-1"))
            await bot.management_command("11", True, "!admins", "123-2")
            self.assertIn("123-2", bot.say.await_args.args[0])
            self.assertIn("@Helper", bot.say.await_args.args[0])
            await bot.management_command("11", True, "!removeadmin", "Helper 123-2")
            self.assertFalse(self.permissions.is_admin("22", "123-2"))

    async def test_invalid_remote_scope_and_unverified_actor_cannot_change_grants(self):
        bot = self.make_bot()
        with patch("bot.resolve_username", new=AsyncMock(return_value=("22", "Helper"))) as lookup:
            for query in ("Helper 999-9", "Helper not-a-room", "Helper 123-2 extra"):
                await bot.management_command("11", True, "!addadmin", query)
            await bot.management_command("11", False, "!addadmin", "Helper 123-2")
            await bot.management_command("22", True, "!addadmin", "Helper 123-2")
            lookup.assert_not_awaited()
        self.assertFalse(self.permissions.is_admin("22", "123-2"))

    async def test_revoked_mod_is_rechecked_after_username_lookup(self):
        bot = self.make_bot()
        async def resolve(api, query):
            self.permissions.manage(OWNER_ID, True, "!removemod", "11")
            return "22", "Helper"
        with patch("bot.resolve_username", new=resolve):
            await bot.management_command("11", True, "!addadmin", "Helper 123-2")
        self.assertFalse(self.permissions.is_admin("22", "123-2"))

    async def test_removed_mod_keeps_only_explicit_room_admin_roles(self):
        p = self.permissions
        p.manage(OWNER_ID, True, "!addadmin", "11", "Moderator", room="123-1")
        p.manage(OWNER_ID, True, "!removemod", "11")
        self.assertIsNotNone(p.authorization_error("11", True, "!join", "123-1"))
        self.assertIsNotNone(p.authorization_error("11", True, "!addadmin", "123-1"))
        self.assertIsNone(p.authorization_error("11", True, "!block", "123-1"))
        self.assertIsNotNone(p.authorization_error("11", True, "!block", "123-2"))

    async def test_command_aliases_use_the_new_role(self):
        for old, new in (("addroomadmin", "addmod"), ("removeroomadmin", "removemod"), ("roomadmins", "mods")):
            self.assertEqual(normalize_management_message("/" + old + " User"), "!" + new + " User")
            self.assertEqual(normalize_management_message("!" + old + " User"), "!" + new + " User")
        self.assertEqual(normalize_management_message("/ADDMOD User"), "!addmod User")
