import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from bot import Bot, Radio, encode


class SongChoices(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.blocked = set()
        self.radio = Radio()
        self.bot = Bot(self.radio, permissions=SimpleNamespace(allowed=lambda uid: uid not in self.blocked))
        self.bot.members = {"1", "2"}
        self.bot.say = AsyncMock()
        self.choices = [{"title": "Song " + str(n), "url": f"https://www.youtube.com/watch?v=abcdefghij{n}"}
                        for n in range(1, 6)]
        self.radio.search = AsyncMock(return_value=self.choices)
        self.radio.lookup = AsyncMock(side_effect=lambda url: {"title": url[-1], "url": url, "duration": 120})

    async def run_commands(self, *commands):
        worker = asyncio.create_task(self.bot.command_worker())
        try:
            for uid, text in commands:
                self.bot.commands.put_nowait((uid, text, True))
            await asyncio.wait_for(self.bot.commands.join(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_search_then_immediate_number_and_duplicate(self):
        await self.run_commands(("1", "!play billie eilish"), ("1", "3"), ("1", "3"))
        self.radio.search.assert_awaited_once_with("billie eilish")
        self.radio.lookup.assert_awaited_once_with(self.choices[2]["url"])
        self.assertEqual(self.radio.queue.qsize(), 1)
        self.assertNotIn("1", self.bot.song_choices)
        message = self.bot.say.await_args_list[0].args[0]
        self.assertIn("1. Song 1", message)
        self.assertIn("5. Song 5", message)
        self.assertLessEqual(len(message), 450)

    async def test_users_and_rooms_cannot_pick_each_others_choices(self):
        await self.bot.play_request("1", "artist")
        await self.run_commands(("2", "1"))
        self.assertTrue(self.radio.queue.empty())
        other_room = Bot(Radio(), room="123-2", permissions=self.bot.permissions)
        self.assertFalse(other_room.song_choices)
        await self.bot.play_request("2", "other artist")
        await self.bot.pick_song("2", "2")
        self.assertIn("1", self.bot.song_choices)
        self.assertNotIn("2", self.bot.song_choices)

    async def test_expired_out_of_range_and_blocked_selection(self):
        await self.bot.play_request("1", "artist")
        await self.run_commands(("1", "9"))
        self.assertTrue(self.radio.queue.empty())
        self.assertIn("1", self.bot.song_choices)
        self.blocked.add("1")
        await self.run_commands(("1", "1"))
        self.radio.lookup.assert_not_awaited()
        self.blocked.clear()
        self.bot.song_choices["1"] = (time.monotonic() - 1, self.choices)
        await self.run_commands(("1", "1"))
        self.assertNotIn("1", self.bot.song_choices)
        self.radio.lookup.assert_not_awaited()

    async def test_direct_link_single_result_and_new_search(self):
        await self.bot.play_request("1", "artist")
        self.radio.search.return_value = self.choices[:2]
        await self.bot.play_request("1", "new artist")
        self.assertEqual(len(self.bot.song_choices["1"][1]), 2)
        await self.bot.play_request("1", self.choices[4]["url"])
        self.radio.lookup.assert_awaited_with(self.choices[4]["url"])
        self.assertNotIn("1", self.bot.song_choices)
        self.radio.search.return_value = self.choices[:1]
        await self.bot.play_request("2", "specific song")
        self.radio.lookup.assert_awaited_with(self.choices[0]["url"])

    async def test_full_queue_keeps_choice_and_block_during_lookup_prevents_queue(self):
        await self.bot.play_request("1", "artist")
        for _ in range(30):
            self.radio.queue.put_nowait({"title": "existing"})
        await self.bot.pick_song("1", "1")
        self.assertIn("1", self.bot.song_choices)
        self.radio.lookup.assert_not_awaited()
        while not self.radio.queue.empty():
            self.radio.queue.get_nowait()
        async def block_during_lookup(url):
            self.blocked.add("1")
            return {"title": "blocked", "url": url}
        self.radio.lookup.side_effect = block_during_lookup
        await self.bot.pick_song("1", "1")
        self.assertTrue(self.radio.queue.empty())

    async def test_receive_routes_numbers_only_for_requester(self):
        self.bot.user_id = "123"
        self.bot.queue = "room-queue"
        self.bot.chat_ready = True
        await self.bot.play_request("1", "artist")
        packets = [{"record": "msg_g2c_send_message", "queue": "room-queue", "mount": "messages",
                    "user_id": uid, "message": encode({"userId": uid, "message": "1"})}
                   for uid in ("2", "1")]
        class Socket:
            async def __aiter__(self):
                for packet in packets:
                    yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(packet))
        self.bot.ws = Socket()
        await self.bot.receive()
        self.assertEqual(self.bot.commands.qsize(), 1)
        self.assertEqual(self.bot.commands.get_nowait(), ("1", "1", True))


class SearchResults(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_search_filters_results_and_resolves_only_selected_later(self):
        entries = [None, {"id": "bad"}, {"id": "abcdefghij0", "duration": 901},
                   {"id": "abcdefghij0", "is_live": True},
                   {"id": "abcdefghij0", "live_status": "is_upcoming"}]
        entries += [{"id": f"abcde{n:06d}", "title": "Song\n" + str(n), "duration": 180} for n in range(12)]
        entries.insert(7, entries[6])  # Duplicate does not consume a choice.
        with patch("bot.capture", AsyncMock(return_value=json.dumps({"entries": entries}))) as capture:
            results = await Radio().search("billie eilish")
        self.assertEqual(len(results), 10)
        self.assertEqual(results[0]["title"], "Song 0")
        self.assertTrue(all(r["url"].startswith("https://www.youtube.com/watch?v=") for r in results))
        self.assertIn("--flat-playlist", capture.await_args.args)
        self.assertEqual(capture.await_args.args[-1], "ytsearch10:billie eilish")

    async def test_empty_results_and_unsupported_urls(self):
        with patch("bot.capture", AsyncMock(return_value='{"entries": []}')):
            with self.assertRaises(ValueError):
                await Radio().search("no results")
        for query in ("https://example.com/audio", "a\nb", "a" * 251):
            with self.assertRaises(ValueError):
                Radio.lookup_target(query)
