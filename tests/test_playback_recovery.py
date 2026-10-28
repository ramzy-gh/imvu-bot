import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import Radio


class PlaybackRecovery(unittest.IsolatedAsyncioTestCase):
    async def decode(self, lengths, codes, track, skip_on_refresh=False):
        radio = Radio()
        radio.pcm = asyncio.Queue(maxsize=100)
        radio.lookup = AsyncMock(return_value={"url": "https://fresh-source"})
        if skip_on_refresh:
            async def refresh(_):
                radio.interrupt()
                await asyncio.sleep(30)
            radio.lookup.side_effect = refresh
        attempts = []
        def arguments(value):
            attempts.append(value)
            return ["decoder"]
        radio.decoder_args = arguments
        async def create(*args, **kwargs):
            index = len(attempts) - 1
            reader = asyncio.StreamReader()
            reader.feed_data(bytes([index + 1]) * lengths[index])
            reader.feed_eof()
            return SimpleNamespace(stdout=reader, returncode=codes[index],
                                   wait=AsyncMock(), communicate=AsyncMock())
        stop = asyncio.create_task(radio.skip.wait())
        with patch("bot.asyncio.create_subprocess_exec", side_effect=create):
            try:
                await asyncio.wait_for(radio.decode_track(track, stop), 8)
            finally:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)
        return radio, attempts

    async def test_clean_early_eof_refreshes_url_and_resumes_exact_sample_position(self):
        radio, attempts = await self.decode([61740, 467460], [0, 0], {
            "url": "https://old-source", "source": "https://www.youtube.com/watch?v=abcdefghijk",
            "duration": 3, "title": "test"})
        self.assertEqual(len(attempts), 2)
        self.assertAlmostEqual(attempts[1]["offset"], 0.35)
        self.assertEqual(attempts[1]["url"], "https://fresh-source")
        self.assertEqual(radio.source_retries, 1)
        self.assertIsNone(radio.last_error)
        chunks = []
        while not radio.pcm.empty():
            chunks.append(radio.pcm.get_nowait())
        self.assertEqual(sum(c.count(1) for c in chunks), 61740)
        self.assertEqual(sum(c.count(2) for c in chunks), 467460)

    async def test_broken_source_has_bounded_retries_and_visible_error(self):
        radio, attempts = await self.decode([0] * 4, [1] * 4,
                                            {"url": "https://bad-source", "duration": 120})
        self.assertEqual(len(attempts), 4)
        self.assertEqual(radio.source_retries, 3)
        self.assertIn("ended early", radio.last_error)

    async def test_skip_cancels_source_refresh_without_restarting_song(self):
        radio, attempts = await self.decode([0], [1], {
            "url": "https://old-source", "source": "https://www.youtube.com/watch?v=abcdefghijk",
            "duration": 120}, skip_on_refresh=True)
        self.assertTrue(radio.skip.is_set())
        self.assertEqual(len(attempts), 1)

    async def test_normal_end_does_not_retry_or_repeat_song(self):
        radio, attempts = await self.decode([529200], [0],
                                            {"url": "https://source", "duration": 3})
        self.assertEqual(len(attempts), 1)
        self.assertEqual(radio.source_retries, 0)
