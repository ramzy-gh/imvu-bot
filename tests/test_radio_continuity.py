import array
import asyncio
import math
import os
import shutil
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

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
        slow_listener = asyncio.Queue(maxsize=1)
        radio.listeners.add(slow_listener)
        output = bytearray()
        slow_output = bytearray()
        async def collect():
            while True:
                output.extend(await listener.get())
        async def collect_slowly():
            while True:
                await asyncio.sleep(0.15)
                slow_output.extend(await slow_listener.get())
        with patch.dict(os.environ, {"FFMPEG_BINARY": FFMPEG}):
            collector = asyncio.create_task(collect())
            slow_collector = asyncio.create_task(collect_slowly())
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
                self.assertIn(slow_listener, radio.listeners)
                self.assertGreater(radio.listener_resyncs, 0)
                self.assertIsNone(radio.last_error)
            finally:
                task.cancel()
                collector.cancel()
                slow_collector.cancel()
                await asyncio.gather(task, collector, slow_collector, return_exceptions=True)
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
        decoder = await asyncio.create_subprocess_exec(FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        recovered_pcm, errors = await decoder.communicate(bytes(slow_output))
        self.assertEqual(decoder.returncode, 0, errors.decode())
        self.assertEqual(errors, b"")
        self.assertGreater(len(recovered_pcm), 0)

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


class RadioHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_probe_startup_and_overflow_preserve_stream(self):
        radio = Radio()
        app = web.Application()
        app.router.add_get("/stream.mp3", radio.stream)
        async with TestClient(TestServer(app)) as client:
            async with asyncio.timeout(2):
                async with client.head("/stream.mp3") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.read(), b"")
                    self.assertFalse(radio.listeners)
                await radio.broadcast(b"startup")
                async with client.get("/stream.mp3", headers={"Range": "bytes=0-"}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Accept-Ranges"], "none")
                    self.assertEqual(await response.content.readexactly(7), b"startup")
                    # No event-loop yield: force the listener queue to overflow.
                    for _ in range(65):
                        await radio.broadcast(b"x")
                    self.assertEqual(radio.listener_resyncs, 1)
                    self.assertEqual(len(radio.listeners), 1)
                    self.assertEqual(await response.content.readexactly(1), b"x")
                    await radio.broadcast(b"next song")
                    self.assertEqual(await response.content.readexactly(9), b"next song")
                    await radio.broadcast(None)
                    self.assertEqual(await response.content.read(), b"")
