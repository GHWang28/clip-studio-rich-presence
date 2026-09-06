"""A stand-in for the Discord desktop client, speaking the real IPC protocol."""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

HEADER = struct.Struct("<II")


def _make_socket_dir() -> str:
    import unittest

    for parent in ("/tmp", None):
        try:
            directory = tempfile.mkdtemp(prefix="fakediscord-", dir=parent)
        except OSError:
            continue
        if len(directory) + len("/discord-ipc-0") < 100:
            return directory
        shutil.rmtree(directory, ignore_errors=True)
    raise unittest.SkipTest("no writable directory short enough for a unix socket")

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4


def encode(opcode: int, payload: Dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return HEADER.pack(opcode, len(body)) + body


class FakeDiscord:
    """Listens on <tmpdir>/discord-ipc-0 and records the frames it receives."""

    def __init__(
        self,
        reject_handshake: Optional[str] = None,
        send_ping: bool = False,
    ):
        if not hasattr(socket, "AF_UNIX"):
            import unittest

            # Real Discord uses a named pipe on Windows, covered separately.
            raise unittest.SkipTest("no unix socket support on this platform")

        # Unix socket paths are capped at ~104 bytes, so prefer short /tmp
        # paths over the long per-user temporary directory.
        self.dir = Path(_make_socket_dir())
        self.path = self.dir / "discord-ipc-0"
        self.reject_handshake = reject_handshake
        self.send_ping = send_ping

        self.handshakes: List[Dict[str, Any]] = []
        self.commands: List[Dict[str, Any]] = []
        self.pongs: List[Dict[str, Any]] = []
        self.closed_by_client = False

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._server.bind(str(self.path))
        except OSError as exc:
            import unittest

            self._server.close()
            shutil.rmtree(self.dir, ignore_errors=True)
            raise unittest.SkipTest("cannot bind a unix socket here: {}".format(exc))
        self._server.listen(5)
        self._server.settimeout(5.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._saved_tmpdir = os.environ.get("TMPDIR")

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "FakeDiscord":
        os.environ["TMPDIR"] = str(self.dir)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=3.0)
        if self._saved_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = self._saved_tmpdir
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- server ------------------------------------------------------------

    def _serve(self) -> None:
        # Real Discord serves many connections over its lifetime, so keep
        # accepting rather than stopping after the first client disconnects.
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(0.5)
                while not self._stop.is_set():
                    frame = self._read_frame(conn)
                    if frame is None:
                        continue
                    if frame is False:
                        break
                    opcode, payload = frame
                    self._handle(conn, opcode, payload)

    def _read_frame(self, conn: socket.socket) -> Any:
        header = self._read_exact(conn, HEADER.size)
        if header is None:
            return None
        if header is False:
            return False
        opcode, length = HEADER.unpack(header)
        body = self._read_exact(conn, length) if length else b""
        if body is None or body is False:
            return False
        return opcode, (json.loads(body.decode("utf-8")) if body else {})

    def _read_exact(self, conn: socket.socket, count: int) -> Any:
        chunks = []
        remaining = count
        while remaining > 0:
            try:
                chunk = conn.recv(remaining)
            except socket.timeout:
                return None if not chunks else False
            if not chunk:
                return False
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _handle(self, conn: socket.socket, opcode: int, payload: Dict[str, Any]) -> None:
        if opcode == OP_HANDSHAKE:
            self.handshakes.append(payload)
            if self.reject_handshake:
                conn.sendall(encode(OP_CLOSE, {"code": 4000, "message": self.reject_handshake}))
                return
            conn.sendall(
                encode(
                    OP_FRAME,
                    {
                        "cmd": "DISPATCH",
                        "evt": "READY",
                        "data": {
                            "v": 1,
                            "user": {"id": "1", "username": "tester"},
                            "config": {},
                        },
                    },
                )
            )
            if self.send_ping:
                conn.sendall(encode(OP_PING, {"hello": "are you there"}))
            return

        if opcode == OP_PONG:
            self.pongs.append(payload)
            return

        if opcode == OP_CLOSE:
            self.closed_by_client = True
            return

        if opcode == OP_FRAME:
            self.commands.append(payload)
            conn.sendall(
                encode(
                    OP_FRAME,
                    {
                        "cmd": payload.get("cmd"),
                        "evt": None,
                        "nonce": payload.get("nonce"),
                        "data": (payload.get("args") or {}).get("activity"),
                    },
                )
            )

    # -- assertions helpers ------------------------------------------------

    def wait_for_commands(self, count: int, timeout: float = 5.0) -> List[Dict[str, Any]]:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.commands) >= count:
                return self.commands
            time.sleep(0.02)
        return self.commands

    def activities(self) -> List[Any]:
        return [(c.get("args") or {}).get("activity") for c in self.commands]
