# -*- coding: utf-8 -*-
"""Low-level TCP transport for APE laser hardware (picoEmerald, waveScan, …).

Pipeline role
-------------
This is the bottom layer of the multi-Z scan stack. It knows nothing about
wavelengths, Z-stacks, or Olympus — only how to open a socket and exchange
ASCII/SCPI messages with the laser controller.

Adapted from APE's official Python example. Protocol matches
``ape_client_S10531.xlsm``: each command is sent with CRLF; replies end at LF.
Higher-level code should use ``laser_client.Laser`` instead of calling this
directly during a scan.
"""

from __future__ import annotations

import socket
import time
import traceback


class ApeDevice:
    """Stateful TCP session to one APE device."""

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
            self.dev.connect((self.host, self.port))
            self.connected = True
            time.sleep(1)
            print(f"Connected to: {self.host}:{self.port}")
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
        self.dev.send(cmd.encode())

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

            while True:
                chunk = self.dev.recv(1)
                if not chunk:
                    break
                if chunk[0] != 0:
                    answer.extend(chunk)
                if chunk[0] == 0x0A:
                    break
            return answer
        except socket.timeout:
            # Set-commands often send no reply; timeout is expected, not a fault.
            raise RuntimeError("[Receive] Error while reading data") from None
        except Exception:
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
        """Send *command* and return the reply (text or SCPI block if *block*)."""
        self.send(command)
        if block:
            return self.read_scpi()
        return self.receive().decode().rstrip()

    def idn(self) -> str:
        return self.query("*idn?")

    def stb(self) -> str:
        return self.query("*stb?")

    def oper(self) -> str:
        return self.query("*oper?")


# Backwards-compatible alias matching APE's original example name.
ape_device = ApeDevice
