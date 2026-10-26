import asyncio
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from movement import API, Movement, asset_url, participant_seats, skeleton_seats


def bone(name, xyz, parent=-1, q=(0, 0, 0, 1)):
    return {"name": name, "parentIndex": parent,
            "relativeTransform": {"translation": xyz, "rotation": q}}


def seats(room, entries):
    return {"denormalized": {API + f"/chat/chat-{room}/participants/user-{uid}":
            {"data": {"seat_furni_id": fid, "seat_number": seat}} for uid, fid, seat in entries}}


class MovementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.room = "123-1"
        self.bot = SimpleNamespace(room=self.room, user_id="9", connected=True, chat_ready=True,
                                   members={"1", "9"}, permissions=SimpleNamespace(allowed=lambda uid: True))
        self.mover = Movement(self.bot)
        self.positions = {30: (0, 0, 0), 31: (100, 0, 0), 8: (1, 0, 0), 7: (2, 0, 0)}
        self.mover.seat_map = AsyncMock(return_value=self.positions)

    async def test_nearest_free_coordinate_and_only_bot_is_moved(self):
        snapshot = seats(self.room, [("1", 0, 30), ("2", 0, 8), ("9", 0, 31)])
        self.bot.api = AsyncMock(side_effect=[snapshot, snapshot, {}, seats(self.room, [("9", 0, 7)])])
        self.assertIn("Moved", await self.mover.move_near("1"))
        self.bot.api.assert_any_await("POST", "/chat/chat-123-1/participants/user-9",
                                       json={"seat_furni_id": 0, "seat_number": 7})
        self.assertEqual(len([c for c in self.bot.api.await_args_list if c.args[0] == "POST"]), 1)
        self.assertIn("moment", await self.mover.move_near("1"))

    async def test_target_moves_during_asset_loading(self):
        self.bot.api = AsyncMock(side_effect=[seats(self.room, [("1", 0, 30)]), seats(self.room, [("1", 8, 1)])])
        self.assertIn("Try !move again", await self.mover.move_near("1"))
        self.assertFalse(any(c.args[0] == "POST" for c in self.bot.api.await_args_list))

    async def test_no_free_seat_and_permission_denied(self):
        snapshot = seats(self.room, [("1", 0, 30), ("9", 0, 31), ("2", 0, 8), ("3", 0, 7)])
        self.bot.api = AsyncMock(return_value=snapshot)
        self.assertIn("isn't a free spot", await self.mover.move_near("1"))
        self.bot.permissions.allowed = lambda uid: False
        self.bot.api.reset_mock()
        self.assertIsNone(await self.mover.move_near("1"))
        self.bot.api.assert_not_awaited()

    async def test_server_must_confirm_new_seat(self):
        snapshot = seats(self.room, [("1", 0, 30), ("9", 0, 31)])
        self.bot.api = AsyncMock(side_effect=[snapshot, snapshot, {}, snapshot])
        self.assertIn("didn't confirm", await self.mover.move_near("1"))
        self.assertEqual(self.mover.last_move, -1000)

    async def test_manifest_skeleton_and_room_isolation(self):
        room = "/scene/scene-123-1"
        self.bot.api = AsyncMock(return_value={"denormalized": {API + room: {"data": {
            "room_asset_url": "https://asset-server-akm.imvu.com/asset/room/test"}}}})
        mover = Movement(self.bot)
        mover.fetch_asset = AsyncMock(side_effect=[{"objects": [{"skeleton": {"url": "/asset/skeleton/test"}}]},
            {"bones": [bone("Seat030.Sitting", (1, 2, 3))]}])
        self.assertEqual(await mover.seat_map("0"), {30: (1, 2, 3)})
        self.bot.api.assert_awaited_once_with("GET", room)
        self.assertFalse(Movement(self.bot).cache)

    def test_rotated_parent_transforms_and_nonconsecutive_seat_numbers(self):
        data = {"bones": [bone("root", (10, 0, 0), q=(0, 0, math.sqrt(.5), math.sqrt(.5))),
                          bone("Seat030.Sitting", (1, 0, 0), 0), bone("seat8.Standing", (2, 0, 0), 1)]}
        result = skeleton_seats(data)
        self.assertAlmostEqual(result[30][0], 10)
        self.assertAlmostEqual(result[30][1], 1)
        self.assertAlmostEqual(result[8][1], 3)
        with self.assertRaises(ValueError):
            skeleton_seats({"bones": [bone("seat1.Sitting", (0, 0, 0), 0)]})

    def test_cross_room_participants_and_untrusted_asset_urls(self):
        self.assertFalse(participant_seats(seats("123-2", [("1", 0, 30)]), "123-1"))
        for url in ["http://asset-server-akm.imvu.com/asset/test", "https://example.com/asset/test",
                    "https://asset-server-akm.imvu.com/private", "//127.0.0.1/asset/test"]:
            with self.assertRaises(ValueError):
                asset_url(url)
