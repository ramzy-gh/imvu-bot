import array
import asyncio
import math
import os
import shutil
import unittest
from unittest.mock import AsyncMock, patch

from bot import Radio

FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    try:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass


class ToneRadio(Radio):
    def decoder_args(self, track):
        return [os.environ["FFMPEG_BINARY"], "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency={track['frequency']}:sample_rate=44100:duration={track.get('duration', 0.8)}",
                "-ac", "2", "-ar", "44100", "-f", "s16le", "pipe:1"]


@unittest.skipUnless(FFMPEG, "FFmpeg is required for audio integration tests")
class RadioContinuity(unittest.IsolatedAsyncioTestCase):
    async def test_two_tracks_decode_on_one_connection_and_one_encoder(self):
        radio = ToneRadio()
        listener = asyncio.Queue(maxsize=256)
        radio.listeners.add(listener)
        output = bytearray()
        async def collect():
            while True:
                output.extend(await listener.get())
        with patch.dict(os.environ, {"FFMPEG_BINARY": FFMPEG}):
            collector = asyncio.create_task(collect())
            task = asyncio.create_task(radio.run(AsyncMock(side_effect=ConnectionError())))
            try:
                # Idle -> song -> next song -> idle, with the same listener.
                await asyncio.sleep(0.5)
                for frequency in (440, 880):
                    radio.queue.put_nowait({"title": str(frequency), "frequency": frequency})
                await asyncio.wait_for(radio.queue.join(), 10)
                await asyncio.sleep(1)
                self.assertEqual(radio.encoder_starts, 1)
                self.assertEqual(radio.track_starts, 2)
                self.assertIn(listener, radio.listeners)
                self.assertIsNone(radio.last_error)
            finally:
                task.cancel()
                collector.cancel()
                await asyncio.gather(task, collector, return_exceptions=True)
        decoder = await asyncio.create_subprocess_exec(FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "mp3", "-i", "pipe:0", "-ac", "1", "-ar", "44100", "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        pcm, errors = await decoder.communicate(bytes(output))
        self.assertEqual(decoder.returncode, 0, errors.decode())
        self.assertEqual(errors, b"")
        samples = array.array("h", pcm)
        def power(window, frequency):
            coefficient = 2 * math.cos(2 * math.pi * frequency / 44100)
            a = b = 0
            for sample in window:
                a, b = sample + coefficient * a - b, a
            return a*a + b*b - coefficient*a*b
        windows = [samples[i:i+4410] for i in range(0, len(samples)-4410, 4410)]
        for frequency in (440, 880):
            self.assertTrue(any(power(w, frequency) > 1e10 for w in windows), f"Missing {frequency} Hz song")

    async def test_skip_does_not_restart_encoder(self):
        radio = ToneRadio()
        with patch.dict(os.environ, {"FFMPEG_BINARY": FFMPEG}):
            radio.queue.put_nowait({"title": "long", "frequency": 440, "duration": 10})
            radio.queue.put_nowait({"title": "next", "frequency": 880})
            task = asyncio.create_task(radio.run(AsyncMock()))
            try:
                async with asyncio.timeout(5):
                    while radio.track_starts < 1:
                        await asyncio.sleep(0.05)
                await asyncio.sleep(0.4)
                radio.interrupt()
                await asyncio.wait_for(radio.queue.join(), 8)
                self.assertEqual(radio.encoder_starts, 1)
                self.assertEqual(radio.track_starts, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
