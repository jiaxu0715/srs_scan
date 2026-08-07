# -*- coding: utf-8 -*-
"""Minimal TCP client for APE devices (picoEmerald / waveScan / etc.).

Adapted from APE's official Python usage example. Speaks the same ASCII
command protocol used by ape_client_S10531.xlsm (command + CRLF, reply to LF).
"""

from __future__ import annotations

import json
import socket
import time
import traceback

# #region agent log
_DEBUG_LOG_PATHS = [
    "/Users/visitor/Documents/Jiaxu/UofT/qianlab/.cursor/debug-3f003b.log",
    str(__import__("pathlib").Path(__file__).resolve().parent / "debug-3f003b.log"),
]


def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    payload = {
        "sessionId": "3f003b",
        "runId": "pre-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload)
    print(f"[debug] {hypothesis_id} {location}: {message} {data or {}}", flush=True)
    for path in _DEBUG_LOG_PATHS:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass
# #endregion


class ApeDevice:
    def __init__(self, host: str = "127.0.0.1", port: int = 51100, name: str = "APEDevice"):
        self.host = host
        self.port = port
        self.name = name
        self.connected = False
        self.dev: socket.socket | None = None
        self.connect()

    def connect(self) -> None:
        if self.connected:
            raise RuntimeError("[Connect] Already connected")
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("[Connect] Hostname must be a non-empty string")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("[Connect] Port must be an int in 1..65535")

        try:
            self.dev = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # #region agent log
            _dbg("E", "ape_device.py:connect", "socket created, connecting", {"host": self.host, "port": self.port, "timeout": self.dev.gettimeout()})
            # #endregion
            self.dev.connect((self.host, self.port))
            self.connected = True
            time.sleep(1)
            print(f"Connected to: {self.host}:{self.port}")
            # #region agent log
            _dbg("C", "ape_device.py:connect", "connect+sleep done", {"host": self.host, "port": self.port})
            # #endregion
        except Exception:
            traceback.print_exc()
            self.connected = False
            self.dev = None
            raise

    def disconnect(self) -> None:
        if not self.connected or self.dev is None:
            return
        self.dev.close()
        self.connected = False
        self.dev = None

    def send(self, command: str) -> None:
        if not self.connected or self.dev is None:
            raise RuntimeError("[Send] Not connected")
        cmd = command.rstrip() + "\r\n"
        # #region agent log
        _dbg("B", "ape_device.py:send", "about to send", {"command": command, "bytes": len(cmd.encode())})
        # #endregion
        self.dev.send(cmd.encode())
        # #region agent log
        _dbg("B", "ape_device.py:send", "send returned", {"command": command})
        # #endregion

    def receive(self, length: int = -1) -> bytearray:
        if not isinstance(length, int):
            raise TypeError("[Receive] length must be an int")
        if not self.connected or self.dev is None:
            raise RuntimeError("[Receive] Not connected")

        answer = bytearray()
        try:
            if length == 0:
                return answer
            if length > 0:
                remaining = length
                while remaining > 0:
                    chunk = self.dev.recv(remaining)
                    if not chunk:
                        break
                    answer.extend(chunk)
                    remaining -= len(chunk)
                return answer

            # #region agent log
            _dbg("A", "ape_device.py:receive", "enter line-receive loop", {"timeout": self.dev.gettimeout()})
            # #endregion
            bytes_seen = 0
            while True:
                chunk = self.dev.recv(1)
                if not chunk:
                    # #region agent log
                    _dbg("A", "ape_device.py:receive", "recv returned empty (peer closed?)", {"bytes_seen": bytes_seen, "partial": answer.decode("latin1", errors="replace")})
                    # #endregion
                    break
                bytes_seen += 1
                if chunk[0] != 0:
                    answer.extend(chunk)
                if chunk[0] == 0x0A:
                    # #region agent log
                    _dbg("A", "ape_device.py:receive", "got LF terminator", {"bytes_seen": bytes_seen, "answer": answer.decode("latin1", errors="replace")})
                    # #endregion
                    break
                if bytes_seen in (1, 8, 32, 64):
                    # #region agent log
                    _dbg("D", "ape_device.py:receive", "partial reply (no LF yet)", {"bytes_seen": bytes_seen, "last_byte": chunk[0], "partial": answer.decode("latin1", errors="replace")})
                    # #endregion
            return answer
        except Exception as exc:
            # #region agent log
            _dbg("A", "ape_device.py:receive", "receive exception", {"type": type(exc).__name__, "repr": repr(exc), "bytes_seen_partial": answer.decode("latin1", errors="replace")})
            # #endregion
            traceback.print_exc()
            raise RuntimeError("[Receive] Error while reading data") from None

    def read_scpi(self) -> bytearray:
        if not self.connected:
            raise RuntimeError("[Read_SCPI] Not connected")
        if self.receive(1)[0] != ord("#"):
            return bytearray()
        header_len = int(self.receive(1).decode())
        if header_len < 0:
            return bytearray()
        data_len = int(self.receive(header_len).decode())
        if data_len <= 0:
            return bytearray()
        return self.receive(data_len)

    def query(self, command: str, block: bool = False):
        # #region agent log
        _dbg("A", "ape_device.py:query", "query start", {"command": command, "block": block})
        # #endregion
        self.send(command)
        if block:
            result = self.read_scpi()
        else:
            result = self.receive().decode().rstrip()
        # #region agent log
        _dbg("A", "ape_device.py:query", "query complete", {"command": command, "result_preview": str(result)[:200]})
        # #endregion
        return result

    def idn(self) -> str:
        return self.query("*idn?")

    def stb(self) -> str:
        return self.query("*stb?")

    def oper(self) -> str:
        return self.query("*oper?")


# Backwards-compatible alias matching APE's original example name.
ape_device = ApeDevice
