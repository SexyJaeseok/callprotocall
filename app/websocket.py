"""RFC 6455 WebSocket 구현 (표준 라이브러리만 사용).

외부 의존성 없이 http.server 소켓 위에서 WebSocket 핸드셰이크와
프레임 인코딩/디코딩을 직접 처리한다.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import threading

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def compute_accept(key: str) -> str:
    """Sec-WebSocket-Accept 값 계산."""
    sha1 = hashlib.sha1((key + WS_MAGIC).encode("utf-8")).digest()
    return base64.b64encode(sha1).decode("ascii")


class WebSocketConnection:
    """단일 WebSocket 연결.

    BaseHTTPRequestHandler 의 rfile/wfile 버퍼 스트림을 그대로 사용한다.
    (raw 소켓과 버퍼 스트림을 섞어 쓰면 송수신 데이터가 꼬이므로,
    핸들러가 이미 쓰는 동일 스트림으로 통일한다.)
    송신은 lock 으로 직렬화한다.
    """

    def __init__(self, sock, rfile=None, wfile=None):
        self.sock = sock
        self.rfile = rfile
        self.wfile = wfile
        self._send_lock = threading.Lock()
        self.closed = False

    # --- 수신 ---
    def _recv_exact(self, n: int) -> bytes:
        if self.rfile is not None:
            data = self.rfile.read(n)
            if not data or len(data) < n:
                raise ConnectionError("socket closed")
            return data
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf.extend(chunk)
        return bytes(buf)

    def receive(self):
        """다음 텍스트 메시지를 str로 반환. 연결 종료 시 None."""
        try:
            while True:
                frame = self._read_frame()
                if frame is None:
                    return None
                opcode, payload = frame
                if opcode == OP_TEXT:
                    return payload.decode("utf-8", errors="replace")
                if opcode == OP_BINARY:
                    return payload.decode("utf-8", errors="replace")
                if opcode == OP_CLOSE:
                    self.close()
                    return None
                if opcode == OP_PING:
                    self._send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
        except (ConnectionError, OSError):
            self.closed = True
            return None

    def _read_frame(self):
        header = self._recv_exact(2)
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if masked else b""
        payload = bytearray(self._recv_exact(length))
        if masked:
            for i in range(length):
                payload[i] ^= mask[i % 4]
        return opcode, bytes(payload)

    # --- 송신 ---
    def _send_frame(self, opcode: int, payload: bytes):
        if self.closed:
            return
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack(">H", n))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", n))
        frame = bytes(header) + payload
        with self._send_lock:
            try:
                if self.wfile is not None:
                    self.wfile.write(frame)
                    self.wfile.flush()
                else:
                    self.sock.sendall(frame)
            except (ConnectionError, OSError, ValueError):
                self.closed = True

    def send(self, text: str):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def close(self):
        if not self.closed:
            self._send_frame(OP_CLOSE, b"")
            self.closed = True
        try:
            if self.wfile is not None:
                try:
                    self.wfile.flush()
                except (OSError, ValueError):
                    pass
            self.sock.close()
        except OSError:
            pass
