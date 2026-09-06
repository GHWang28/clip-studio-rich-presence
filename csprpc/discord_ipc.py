"""A minimal Discord IPC client for macOS and Windows.

Discord's desktop client listens on an endpoint named ``discord-ipc-0`` through
``discord-ipc-9``: a unix domain socket on macOS, a named pipe on Windows.
The conversation itself is identical on both, an 8 byte header of two little
endian uint32s (opcode, payload length) followed by JSON, so only the transport
differs and there is no third party dependency here.
"""

from __future__ import annotations

import json
import os
import select
import socket
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

RPC_VERSION = 1

# Discord silently drops presence updates sent more often than this.
MIN_UPDATE_INTERVAL = 15.0

_HEADER = struct.Struct("<II")
_MAX_PAYLOAD = 1 << 20
_POLL_SLEEP = 0.01

IS_WINDOWS = sys.platform == "win32"


class IPCError(Exception):
    """Base class for IPC failures."""


class DiscordNotRunning(IPCError):
    """No Discord IPC endpoint could be found or connected to."""


class DiscordClosed(IPCError):
    """Discord asked us to go away, or the connection died mid-conversation."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


# -- transports -----------------------------------------------------------


class _Transport:
    """The few operations the protocol needs from a byte stream."""

    endpoint = ""

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def recv_exact(self, count: int, timeout: float) -> bytes:
        raise NotImplementedError

    def readable(self, timeout: float) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _UnixSocketTransport(_Transport):
    def __init__(self, path: str, sock: socket.socket):
        self.endpoint = path
        self._sock = sock

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv_exact(self, count: int, timeout: float) -> bytes:
        self._sock.settimeout(timeout)
        chunks: List[bytes] = []
        remaining = count
        while remaining > 0:
            try:
                chunk = self._sock.recv(remaining)
            except socket.timeout:
                raise DiscordClosed("timed out reading from Discord")
            if not chunk:
                raise DiscordClosed("Discord closed the socket")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def readable(self, timeout: float) -> bool:
        ready, _, _ = select.select([self._sock], [], [], timeout)
        return bool(ready)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class _NamedPipeTransport(_Transport):
    """Windows named pipe.

    Python's ``select`` only accepts sockets on Windows, so readiness is
    checked with PeekNamedPipe and reads are polled up to their deadline.
    """

    def __init__(self, path: str, handle: Any):
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self.endpoint = path
        self._file = handle
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.PeekNamedPipe.restype = wintypes.BOOL
        self._kernel32.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._osf = msvcrt.get_osfhandle(handle.fileno())

    def _available(self) -> int:
        available = self._wintypes.DWORD(0)
        ok = self._kernel32.PeekNamedPipe(
            self._osf, None, 0, None, self._ctypes.byref(available), None
        )
        if not ok:
            raise DiscordClosed("Discord closed the pipe")
        return int(available.value)

    def send(self, data: bytes) -> None:
        self._file.write(data)
        self._file.flush()

    def recv_exact(self, count: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        chunks: List[bytes] = []
        remaining = count
        while remaining > 0:
            available = self._available()
            if available <= 0:
                if time.monotonic() >= deadline:
                    raise DiscordClosed("timed out reading from Discord")
                time.sleep(_POLL_SLEEP)
                continue
            chunk = self._file.read(min(available, remaining))
            if not chunk:
                raise DiscordClosed("Discord closed the pipe")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def readable(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self._available() > 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SLEEP)

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


# -- endpoint discovery ---------------------------------------------------


def candidate_endpoints() -> List[str]:
    """Every plausible IPC endpoint, in the order we should try them."""
    if IS_WINDOWS:
        return [r"\\.\pipe\discord-ipc-{}".format(index) for index in range(10)]

    bases: List[Path] = []
    seen = set()

    def add_base(value: Optional[str]) -> None:
        if not value:
            return
        path = Path(value)
        if str(path) not in seen:
            seen.add(str(path))
            bases.append(path)

    # macOS puts it in the per-user temporary directory.
    add_base(os.environ.get("TMPDIR"))
    add_base(os.environ.get("XDG_RUNTIME_DIR"))
    add_base(os.environ.get("TMP"))
    add_base(os.environ.get("TEMP"))
    add_base("/tmp")

    # Sandboxed Linux packaging puts it one level deeper. Harmless on macOS.
    nested = ["snap.discord", "app/com.discordapp.Discord", ".flatpak/dev.vencord.Vesktop/xdg-run"]

    endpoints: List[str] = []
    for base in bases:
        for index in range(10):
            endpoints.append(str(base / "discord-ipc-{}".format(index)))
        for suffix in nested:
            for index in range(10):
                endpoints.append(str(base / suffix / "discord-ipc-{}".format(index)))
    return endpoints


def _open_transport(endpoint: str, timeout: float) -> Optional[_Transport]:
    """Connect to one endpoint, or return None if it is not there."""
    if IS_WINDOWS:
        try:
            handle = open(endpoint, "r+b", buffering=0)
        except OSError:
            return None
        try:
            return _NamedPipeTransport(endpoint, handle)
        except OSError:
            handle.close()
            return None

    try:
        if not Path(endpoint).is_socket():
            return None
    except OSError:
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(endpoint)
    except OSError:
        sock.close()
        return None
    return _UnixSocketTransport(endpoint, sock)


def find_endpoint(timeout: float = 1.0) -> Optional[str]:
    """The first reachable Discord IPC endpoint, or None."""
    for endpoint in candidate_endpoints():
        if not IS_WINDOWS:
            # A unix socket can be spotted without connecting to it.
            try:
                if Path(endpoint).is_socket():
                    return endpoint
            except OSError:
                pass
            continue
        # A named pipe cannot be reliably stat'd, so open it and let go.
        transport = _open_transport(endpoint, timeout)
        if transport is not None:
            transport.close()
            return endpoint
    return None


# -- client ---------------------------------------------------------------


class DiscordIPC:
    """A connection to the local Discord client."""

    def __init__(self, client_id: str, timeout: float = 5.0):
        self.client_id = str(client_id)
        self.timeout = timeout
        self._transport: Optional[_Transport] = None
        self.endpoint: Optional[str] = None
        self.user: Optional[Dict[str, Any]] = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._transport is not None

    def connect(self) -> None:
        """Open the connection and complete the handshake.

        Raises DiscordNotRunning if Discord is not reachable, or DiscordClosed
        if Discord rejects the handshake (most often a bad client_id).
        """
        if self._transport is not None:
            return

        for endpoint in candidate_endpoints():
            transport = _open_transport(endpoint, self.timeout)
            if transport is not None:
                self._transport = transport
                self.endpoint = endpoint
                break

        if self._transport is None:
            raise DiscordNotRunning(
                "no Discord IPC endpoint found - is the Discord desktop app running?"
            )

        try:
            self._handshake()
        except Exception:
            self.close(notify=False)
            raise

    def _handshake(self) -> None:
        self._send(OP_HANDSHAKE, {"v": RPC_VERSION, "client_id": self.client_id})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            opcode, payload = self._read_frame()
            if opcode == OP_CLOSE:
                raise DiscordClosed(
                    payload.get("message") or "Discord closed the connection during handshake",
                    payload.get("code"),
                )
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_FRAME and payload.get("evt") == "READY":
                self.user = (payload.get("data") or {}).get("user")
                return
        raise DiscordClosed("timed out waiting for Discord's READY event")

    def close(self, notify: bool = True) -> None:
        """Close the connection, optionally telling Discord first."""
        transport = self._transport
        self._transport = None
        self.endpoint = None
        self.user = None
        if transport is None:
            return
        if notify:
            try:
                transport.send(_encode(OP_CLOSE, {}))
            except (OSError, IPCError):
                pass
        transport.close()

    def __enter__(self) -> "DiscordIPC":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- commands ----------------------------------------------------------

    def set_activity(self, activity: Optional[Dict[str, Any]], pid: Optional[int] = None) -> None:
        """Set (or with activity=None, clear) the Rich Presence."""
        # Discord clears the presence when the activity key is null.
        args: Dict[str, Any] = {
            "pid": pid if pid is not None else os.getpid(),
            "activity": activity,
        }
        nonce = str(uuid.uuid4())
        self._send(OP_FRAME, {"cmd": "SET_ACTIVITY", "args": args, "nonce": nonce})
        self._await_response(nonce)

    def clear_activity(self, pid: Optional[int] = None) -> None:
        self.set_activity(None, pid=pid)

    def _await_response(self, nonce: str) -> None:
        """Wait briefly for the echo of a command, surfacing any error."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if not self._readable(remaining):
                return  # Discord is not always chatty; do not treat this as fatal.
            opcode, payload = self._read_frame()
            if opcode == OP_CLOSE:
                code = payload.get("code")
                self.close(notify=False)
                raise DiscordClosed(payload.get("message") or "Discord closed the connection", code)
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode != OP_FRAME:
                continue
            if payload.get("evt") == "ERROR":
                data = payload.get("data") or {}
                raise IPCError(
                    "Discord rejected the update: {} (code {})".format(
                        data.get("message", "unknown error"), data.get("code", "?")
                    )
                )
            if payload.get("nonce") == nonce:
                return

    def pump(self) -> None:
        """Service anything Discord sent us unprompted (mainly pings)."""
        while self._readable(0.0):
            opcode, payload = self._read_frame()
            if opcode == OP_CLOSE:
                code = payload.get("code")
                self.close(notify=False)
                raise DiscordClosed(payload.get("message") or "Discord closed the connection", code)
            if opcode == OP_PING:
                self._send(OP_PONG, payload)

    # -- wire format -------------------------------------------------------

    def _readable(self, timeout: float) -> bool:
        if self._transport is None:
            raise DiscordClosed("not connected")
        try:
            return self._transport.readable(timeout)
        except OSError as exc:
            self.close(notify=False)
            raise DiscordClosed("failed to poll Discord: {}".format(exc))

    def _send(self, opcode: int, payload: Dict[str, Any]) -> None:
        if self._transport is None:
            raise DiscordClosed("not connected")
        try:
            # The header and body must go out as one write or Discord drops
            # the connection.
            self._transport.send(_encode(opcode, payload))
        except OSError as exc:
            self.close(notify=False)
            raise DiscordClosed("failed to write to Discord: {}".format(exc))

    def _read_frame(self) -> Tuple[int, Dict[str, Any]]:
        header = self._read_exact(_HEADER.size)
        opcode, length = _HEADER.unpack(header)
        if length > _MAX_PAYLOAD:
            self.close(notify=False)
            raise DiscordClosed("Discord sent an implausibly large frame ({} bytes)".format(length))
        body = self._read_exact(length) if length else b""
        if not body:
            return opcode, {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise DiscordClosed("Discord sent malformed JSON: {}".format(exc))
        return opcode, payload if isinstance(payload, dict) else {}

    def _read_exact(self, count: int) -> bytes:
        if self._transport is None:
            raise DiscordClosed("not connected")
        try:
            return self._transport.recv_exact(count, self.timeout)
        except DiscordClosed:
            self.close(notify=False)
            raise
        except OSError as exc:
            self.close(notify=False)
            raise DiscordClosed("failed to read from Discord: {}".format(exc))


def _encode(opcode: int, payload: Dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _HEADER.pack(opcode, len(body)) + body
