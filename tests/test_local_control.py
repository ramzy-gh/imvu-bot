import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from local_control import LocalControl, send_command


class LocalControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "control.json"
        self.command = AsyncMock(return_value="Join requested for 47377071-5395")
        self.control = LocalControl(self.command, self.path)
        await self.control.start()

    async def asyncTearDown(self):
        await self.control.close()
        self.temp.cleanup()

    async def test_control_is_loopback_only(self):
        self.assertEqual(self.control.server.sockets[0].getsockname()[0], "127.0.0.1")
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_join_reaches_running_bot(self):
        result = await send_command("join 47377071-5395", self.path)
        self.assertTrue(result["ok"])
        self.command.assert_awaited_once_with("join 47377071-5395")

    async def test_missing_or_wrong_token_does_not_execute(self):
        data = json.loads(self.path.read_text())
        for payload in ({"command": "join 47377071-5395"},
                        {"token": "wrong", "command": "leave 330286535-177"}):
            reader, writer = await asyncio.open_connection("127.0.0.1", data["port"])
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            self.assertFalse(json.loads(await reader.readline())["ok"])
            writer.close()
            await writer.wait_closed()
        self.command.assert_not_awaited()

    async def test_invalid_room_reported_without_breaking_server(self):
        self.command.side_effect = ValueError("Invalid room ID")
        result = await send_command("join invalid", self.path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Invalid room ID")
        self.command.side_effect = None
        self.assertTrue((await send_command("rooms", self.path))["ok"])

    async def test_cli_works_from_separate_process(self):
        env = {**os.environ, "CONTROL_STATE_PATH": str(self.path)}
        proc = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).resolve().parents[1] / "botctl.py"), "join", "47377071-5395",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 10)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIn(b"Join requested for 47377071-5395", out)
        self.command.assert_awaited_once_with("join 47377071-5395")

    async def test_close_removes_discovery_file(self):
        await self.control.close()
        self.assertFalse(self.path.exists())
