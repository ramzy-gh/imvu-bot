import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

from bot import Radio


class MusicHeaders(unittest.IsolatedAsyncioTestCase):
    async def decode_script(self, script, duration=0.2):
        radio = Radio()
        radio.pcm = asyncio.Queue(maxsize=100)
        radio.decoder_args = lambda _: [sys.executable, "-u", "-c", script]
        stop = asyncio.create_task(radio.skip.wait())
        try:
            await asyncio.wait_for(radio.decode_track({"duration": duration}, stop), 8)
        finally:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)
        return radio

    async def test_real_subprocess_stderr_cannot_block_audio_or_cleanup(self):
        radio = await self.decode_script(
            "import sys; sys.stderr.buffer.write(b'warning ' * 50000); "
            "sys.stderr.flush(); sys.stdout.buffer.write(bytes(35280))")
        self.assertEqual(radio.pcm.qsize(), 2)
        self.assertIsNone(radio.last_error)
        self.assertIsNone(radio.proc)

    async def test_real_decoder_failure_logs_reason_without_signed_url(self):
        with self.assertLogs("ramsei", level="WARNING") as logs:
            radio = await self.decode_script(
                "import sys; sys.stderr.write('Server returned 403 Forbidden https://secret.example/token'); "
                "sys.exit(1)", duration=120)
        self.assertIn("reason=http_403", str(logs.output))
        self.assertNotIn("secret.example", str(logs.output))
        self.assertEqual(radio.source_retries, 3)
        self.assertIsNone(radio.proc)

    async def test_skip_reaps_decoder_with_open_error_pipe(self):
        radio = Radio()
        radio.decoder_args = lambda _: [sys.executable, "-u", "-c", "import time; time.sleep(100)"]
        stop = asyncio.create_task(radio.skip.wait())
        decode = asyncio.create_task(radio.decode_track({"duration": 120}, stop))
        try:
            async with asyncio.timeout(3):
                while radio.proc is None:
                    await asyncio.sleep(0.01)
            proc = radio.proc
            radio.interrupt()
            await asyncio.wait_for(decode, 4)
            self.assertIsNotNone(proc.returncode)
            self.assertIsNone(radio.proc)
        finally:
            decode.cancel()
            stop.cancel()
            await asyncio.gather(decode, stop, return_exceptions=True)

    async def test_relay_decoder_sends_browser_user_agent_before_input(self):
        track = Radio.relay_track({
            "videoId": "abcdefghijk", "lengthSeconds": 120, "title": "Song",
            "adaptiveFormats": [{"itag": 140, "type": "audio/mp4",
                                 "url": "https://yt.chocolatemoo53.com/videoplayback?token=private"}],
        }, "abcdefghijk")
        args = Radio().decoder_args(track)
        self.assertEqual(args[args.index("-user_agent") + 1], "Mozilla/5.0")
        self.assertLess(args.index("-user_agent"), args.index("-i"))
        self.assertLess(args.index("-i"), args.index("-ss"))

    async def test_extractor_headers_reach_decoder_without_login_credentials(self):
        data = {"url": "https://audio.example/song", "duration": 120, "title": "Song",
                "http_headers": {"user-agent": "Source media agent", "Referer": "https://source.example/",
                                 "Origin": "https://source.example", "Cookie": "private-session",
                                 "Authorization": "private-token", "Accept": "audio/*\r\nInjected: value"}}
        with patch("bot.capture", AsyncMock(return_value=json.dumps(data))):
            track = await Radio().lookup("https://soundcloud.com/artist/song")
        args = Radio().decoder_args(track)
        self.assertEqual(args[args.index("-user_agent") + 1], "Source media agent")
        self.assertIn("Referer: https://source.example/\r\n", args[args.index("-headers") + 1])
        self.assertIn("Origin: https://source.example\r\n", args[args.index("-headers") + 1])
        for secret in ("private-session", "private-token", "Injected"):
            self.assertNotIn(secret, str(args))

    async def test_malformed_headers_use_safe_default(self):
        for headers in (None, [], "invalid", {"User-Agent": "bad\nheader"}, {"User-Agent": ""},
                        {"User-Agent": "x" * 2049}, {"User-Agent": "bad\x00header"}):
            self.assertEqual(Radio.media_headers(headers), {"User-Agent": "Mozilla/5.0"})

    async def test_noisy_decoder_errors_are_drained_and_bounded(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"private-signed-url " * 20000 + b"Server returned 403 Forbidden")
        reader.feed_eof()
        tail = bytearray()
        await Radio.collect_decoder_errors(reader, tail)
        self.assertLessEqual(len(tail), 8192)
        self.assertEqual(Radio.decoder_error_reason(tail), "http_403")

    async def test_diagnostic_categories_never_return_raw_urls(self):
        for message, expected in (
                (b"HTTP error 429 Too Many Requests https://private.example/token", "http_429"),
                (b"Protocol 'http' not on whitelist", "protocol_not_allowed"),
                (b"Invalid data found when processing input https://private.example/token", "invalid_media"),
                (b"Connection timed out", "source_timeout"),
                (b"certificate verify failed", "tls_error"),
                (b"Option not found", "decoder_option_error"),
                (b"https://private.example/token failed", "decoder_error"),
                (b"", "early_eof_or_stall")):
            self.assertEqual(Radio.decoder_error_reason(message), expected)
