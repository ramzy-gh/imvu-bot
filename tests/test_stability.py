import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from yarl import URL

from bot import API, Bot, LoginPaused, Radio, supervise
from room_control import Authentication, RoomManager


class StabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_failure_and_failed_error_reply_do_not_kill_commands(self):
        bot = Bot(Radio(), permissions=SimpleNamespace(allowed=lambda uid, room: True))
        bot.members = {"1", "2"}
        bot.radio.search = AsyncMock(side_effect=OSError("extractor spawn failed"))
        bot.say = AsyncMock(side_effect=ConnectionError("chat unavailable"))
        worker = asyncio.create_task(bot.command_worker())
        try:
            bot.commands.put_nowait(("1", "!play song", True))
            await asyncio.wait_for(bot.commands.join(), 1)
            self.assertFalse(worker.done())
            bot.say.side_effect = None
            bot.commands.put_nowait(("2", "!np", True))
            await asyncio.wait_for(bot.commands.join(), 1)
            bot.say.assert_awaited_with("No song is playing.")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_stuck_lookup_times_out_and_next_command_still_works(self):
        bot = Bot(Radio(), permissions=SimpleNamespace(allowed=lambda uid, room: True))
        bot.members = {"1", "2"}
        bot.radio.LOOKUP_TIMEOUT = 0.02
        async def stuck(_):
            await asyncio.Event().wait()
        bot.radio.lookup = stuck
        bot.say = AsyncMock()
        worker = asyncio.create_task(bot.command_worker())
        try:
            bot.commands.put_nowait(("1", "!play https://youtu.be/abcdefghijk", True))
            bot.commands.put_nowait(("2", "!np", True))
            await asyncio.wait_for(bot.commands.join(), 1)
            self.assertFalse(worker.done())
            self.assertTrue(bot.radio.queue.empty())
            bot.say.assert_awaited_with("No song is playing.")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_failed_track_does_not_poison_queue_accounting(self):
        radio = Radio()
        attempts = []
        async def decode(track, stop):
            attempts.append(track["title"])
            if len(attempts) == 1:
                raise KeyError("malformed media metadata")
        radio.decode_track = decode
        for title in ("broken", "next"):
            radio.queue.put_nowait({"title": title})
        worker = asyncio.create_task(radio.produce_pcm(AsyncMock()))
        try:
            await asyncio.wait_for(radio.queue.join(), 1)
            self.assertEqual(attempts, ["broken", "next"])
            self.assertFalse(worker.done())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_supervisor_recovers_worker_and_honors_shutdown(self):
        entered = asyncio.Event()
        calls = 0
        async def worker():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyError("bad response")
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(supervise(worker, "test", delay=0.01))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(calls, 2)
        self.assertTrue(task.cancelled())

    async def test_network_login_failure_is_not_cached_forever(self):
        auth = Authentication()
        async with aiohttp.ClientSession() as session:
            bot = SimpleNamespace(session=session)
            async def login(force=False):
                if bot._login.await_count == 1:
                    raise aiohttp.ClientConnectionError()
                bot.user_id = "999"
                session.cookie_jar.update_cookies({"osCsid": "test"}, response_url=URL(API))
                session.headers["X-Imvu-Sauce"] = "test-sauce"
            bot._login = AsyncMock(side_effect=login)
            with self.assertRaises(aiohttp.ClientConnectionError):
                await auth.login(bot)
            self.assertIsNone(auth.failure)
            auth.retry_at = 0
            await auth.login(bot)
            self.assertEqual(bot.user_id, "999")
            self.assertEqual(bot._login.await_count, 2)

    async def test_verification_challenge_is_not_retried(self):
        auth = Authentication()
        bot = SimpleNamespace(session=SimpleNamespace(headers={}),
                              _login=AsyncMock(side_effect=LoginPaused("verification_required", "verify")))
        for _ in range(2):
            with self.assertRaises(LoginPaused):
                await auth.login(bot)
        self.assertEqual(bot._login.await_count, 1)

    async def test_stalled_encoder_recovers_without_disconnecting_listener(self):
        await self.check_encoder_recovery("read")

    async def test_blocked_encoder_input_recovers_without_disconnecting_listener(self):
        await self.check_encoder_recovery("write")

    async def check_encoder_recovery(self, stall):
        radio = Radio()
        radio.IO_TIMEOUT = 0.03
        radio.RECOVERY_DELAY = 0.01
        listener = asyncio.Queue(maxsize=64)
        radio.listeners.add(listener)
        produced = asyncio.Event()
        producer_started = 0
        async def producer(announce):
            nonlocal producer_started
            producer_started += 1
            radio.current = {"title": "still playing"}
            produced.set()
            await asyncio.Event().wait()
        radio.produce_pcm = producer
        processes = []
        def make_process(*args, **kwargs):
            reader = asyncio.StreamReader()
            proc = SimpleNamespace(stdout=reader, returncode=None,
                                   stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()))
            def terminate():
                proc.returncode = 0
                reader.feed_eof()
            proc.terminate = proc.kill = terminate
            proc.communicate = AsyncMock(return_value=(b"", b""))
            processes.append(proc)
            if len(processes) == 1 and stall == "write":
                async def blocked_drain():
                    await asyncio.Event().wait()
                proc.stdin.drain = blocked_drain
            if len(processes) > 1:
                # One valid MPEG-1 Layer III 128 kbps / 44.1kHz frame.
                reader.feed_data(bytes.fromhex("fffb9000") + bytes(413))
            return proc
        with patch("bot.asyncio.create_subprocess_exec", side_effect=make_process):
            worker = asyncio.create_task(radio.run(AsyncMock()))
            try:
                await asyncio.wait_for(produced.wait(), 1)
                frame = await asyncio.wait_for(listener.get(), 1)
                self.assertEqual(len(frame), 417)
                self.assertEqual(radio.encoder_starts, 2)
                self.assertEqual(radio.recoveries, 1)
                self.assertEqual(producer_started, 1)
                self.assertEqual(radio.current["title"], "still playing")
                self.assertIn(listener, radio.listeners)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(all(p.returncode == 0 for p in processes))

    async def test_command_worker_restart_leaves_room_connection_running(self):
        connected = asyncio.Event()
        recovered = asyncio.Event()
        calls = 0
        async def connection():
            connected.set()
            await asyncio.Event().wait()
        async def commands():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyError("unexpected command response")
            recovered.set()
            await asyncio.Event().wait()
        idle = asyncio.Event().wait
        authenticated = asyncio.Event()
        authenticated.set()
        radio = SimpleNamespace(broadcast=AsyncMock())
        async def audio(_):
            await idle()
        radio.run = audio
        bot = SimpleNamespace(room="123-1", session=None, run=AsyncMock(side_effect=connection),
                              command_worker=commands, social=SimpleNamespace(run=idle),
                              authenticated=authenticated, radio=radio, say=AsyncMock())
        manager = SimpleNamespace(room_health_changed=None)
        with patch("room_control.supervise", new=lambda factory, label: supervise(factory, label, 0.01)):
            task = asyncio.create_task(RoomManager.serve(manager, bot))
            try:
                await asyncio.wait_for(connected.wait(), 1)
                await asyncio.wait_for(recovered.wait(), 1)
                self.assertEqual(bot.run.await_count, 1)
                self.assertFalse(task.done())
                radio.broadcast.assert_not_awaited()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
