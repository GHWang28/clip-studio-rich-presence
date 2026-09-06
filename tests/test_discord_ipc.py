import unittest
from unittest import mock

from csprpc import discord_ipc
from csprpc.discord_ipc import DiscordClosed, DiscordIPC, DiscordNotRunning, find_endpoint
from tests.fake_discord import FakeDiscord


class EndpointDiscoveryTest(unittest.TestCase):
    def test_windows_uses_named_pipes(self):
        with mock.patch.object(discord_ipc, "IS_WINDOWS", True):
            endpoints = discord_ipc.candidate_endpoints()
        self.assertEqual(len(endpoints), 10)
        self.assertEqual(endpoints[0], r"\\.\pipe\discord-ipc-0")
        self.assertEqual(endpoints[9], r"\\.\pipe\discord-ipc-9")

    def test_posix_searches_the_temporary_directories(self):
        with mock.patch.object(discord_ipc, "IS_WINDOWS", False):
            endpoints = discord_ipc.candidate_endpoints()
        # Which directories and names are searched is the point here; the
        # separator is just an artifact of the host running the test.
        endpoints = [e.replace("\\", "/") for e in endpoints]
        self.assertTrue(any(e.endswith("/discord-ipc-0") for e in endpoints))
        self.assertTrue(any(e.startswith("/tmp/") for e in endpoints))

    def test_frame_encoding_is_the_documented_header(self):
        import struct

        frame = discord_ipc._encode(discord_ipc.OP_HANDSHAKE, {"v": 1})
        opcode, length = struct.unpack("<II", frame[:8])
        self.assertEqual(opcode, 0)
        self.assertEqual(length, len(frame) - 8)
        self.assertEqual(frame[8:], b'{"v":1}')


class DiscordIPCTest(unittest.TestCase):
    def test_handshake_and_set_activity(self):
        with FakeDiscord() as server:
            self.assertEqual(find_endpoint(), str(server.path))

            ipc = DiscordIPC("123456789")
            ipc.connect()
            try:
                self.assertTrue(ipc.connected)
                self.assertEqual(ipc.user, {"id": "1", "username": "tester"})
                self.assertEqual(server.handshakes[0], {"v": 1, "client_id": "123456789"})

                ipc.set_activity({"details": "Sketch.clip", "state": "12m today"})
            finally:
                ipc.close()

            server.wait_for_commands(1)
            self.assertEqual(len(server.commands), 1)
            command = server.commands[0]
            self.assertEqual(command["cmd"], "SET_ACTIVITY")
            self.assertEqual(command["args"]["activity"]["details"], "Sketch.clip")
            self.assertIsInstance(command["args"]["pid"], int)
            self.assertTrue(command["nonce"])

    def test_clear_activity_sends_null(self):
        with FakeDiscord() as server:
            with DiscordIPC("123456789") as ipc:
                ipc.clear_activity()
            server.wait_for_commands(1)
            self.assertIsNone(server.commands[0]["args"]["activity"])

    def test_responds_to_ping_with_pong(self):
        import time

        with FakeDiscord(send_ping=True) as server:
            with DiscordIPC("123456789") as ipc:
                deadline = time.monotonic() + 5.0
                while not server.pongs and time.monotonic() < deadline:
                    ipc.pump()  # the ping may still be in flight
                    time.sleep(0.02)
            self.assertEqual(server.pongs, [{"hello": "are you there"}])

    def test_rejected_handshake_raises(self):
        with FakeDiscord(reject_handshake="Invalid Client ID") as server:
            ipc = DiscordIPC("nope")
            with self.assertRaises(DiscordClosed) as caught:
                ipc.connect()
            self.assertEqual(caught.exception.code, 4000)
            self.assertIn("Invalid Client ID", str(caught.exception))
            self.assertFalse(ipc.connected)
            self.assertEqual(len(server.handshakes), 1)

    def test_no_socket_raises_not_running(self):
        import os
        import tempfile

        saved = os.environ.get("TMPDIR")
        # No dir=: there is no /tmp on Windows.
        empty = tempfile.mkdtemp(prefix="nodiscord-")
        os.environ["TMPDIR"] = empty
        # /tmp itself is in the search path, so only assert when it is clean.
        try:
            if find_endpoint() is not None:
                self.skipTest("a real Discord socket is present on this machine")
            with self.assertRaises(DiscordNotRunning):
                DiscordIPC("123456789").connect()
        finally:
            if saved is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = saved
            os.rmdir(empty)


if __name__ == "__main__":
    unittest.main()
