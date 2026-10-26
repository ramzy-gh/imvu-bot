import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from permissions import OWNER_ID, Permissions, ROOM_COMMANDS, normalize_management_message


class RoomAccessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "permissions.json"
        self.env = patch.dict(os.environ, {"PERMISSIONS_PATH": str(self.path), "DATABASE_URL": "", "ADMIN_IDS": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_owner_grants_and_revokes_persistent_separate_role(self):
        p = Permissions()
        p.manage(OWNER_ID, True, "!addroomadmin", "123", username="C4MU")
        loaded = Permissions()
        self.assertEqual(loaded.room_admins, {"123"})
        self.assertNotIn("123", loaded.admins)
        self.assertIn("@C4MU", loaded.manage(OWNER_ID, True, "!roomadmins"))
        for cmd in ROOM_COMMANDS:
            self.assertIsNone(loaded.authorization_error("123", True, cmd))
        self.assertIsNotNone(loaded.authorization_error("123", True, "!addadmin"))
        loaded.manage(OWNER_ID, True, "!removeroomadmin", "123")
        self.assertFalse(Permissions().room_admins)
        self.assertIsNotNone(loaded.authorization_error("123", True, "!join"))

    def test_admins_and_delegates_cannot_grant_or_revoke_access(self):
        p = Permissions()
        p.admins.add("456")
        p.room_admins.add("123")
        for actor, verified in [("456", True), ("123", True), (OWNER_ID, False)]:
            for command in ["!addroomadmin", "!removeroomadmin", "!roomadmins"]:
                self.assertIsNotNone(p.authorization_error(actor, verified, command))
            p.manage(actor, verified, "!addroomadmin", "789", username="Visitor")
            p.manage(actor, verified, "!removeroomadmin", "123")
        self.assertEqual(p.room_admins, {"123"})
        self.assertIsNotNone(p.authorization_error("456", True, "!join"))
        self.assertIsNotNone(p.authorization_error("123", False, "!join"))

    def test_older_permissions_files_migrate_without_losing_roles(self):
        self.path.write_text(json.dumps({"admins": ["456"], "blocked": ["789"],
                                         "usernames": {"456": "Admin", "789": "Blocked"}}))
        p = Permissions()
        self.assertFalse(p.room_admins)
        p.manage(OWNER_ID, True, "!addroomadmin", "123", username="RoomHelper")
        loaded = Permissions()
        self.assertEqual(loaded.admins, {"456"})
        self.assertEqual(loaded.blocked, {"789"})
        self.assertEqual(loaded.room_admins, {"123"})

    def test_save_failure_rolls_back_grants_revocations_and_labels(self):
        p = Permissions()
        with patch.object(p.store, "save", side_effect=OSError("unavailable")):
            p.manage(OWNER_ID, True, "!addroomadmin", "123", username="Helper")
        self.assertFalse(p.room_admins)
        self.assertNotIn("123", p.names)
        p.manage(OWNER_ID, True, "!addroomadmin", "123", username="Helper")
        with patch.object(p.store, "save", side_effect=OSError("unavailable")):
            p.manage(OWNER_ID, True, "!removeroomadmin", "123")
        self.assertEqual(p.room_admins, {"123"})
        self.assertEqual(Permissions().room_admins, {"123"})

    def test_blocked_access_and_owner_protection(self):
        p = Permissions()
        p.manage(OWNER_ID, True, "!addroomadmin", "123", username="Helper")
        p.manage(OWNER_ID, True, "!block", "123")
        self.assertIsNotNone(p.authorization_error("123", True, "!join"))
        p.manage(OWNER_ID, True, "!unblock", "123")
        self.assertIsNone(p.authorization_error("123", True, "!join"))
        p.manage(OWNER_ID, True, "!removeroomadmin", OWNER_ID)
        self.assertIsNone(p.authorization_error(OWNER_ID, True, "!join"))
        self.assertEqual(normalize_management_message("/addroomadmin C4MU"), "!addroomadmin C4MU")
