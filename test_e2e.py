"""End-to-end 스모크 테스트 (표준 라이브러리만).

서버가 http://localhost:8000 에서 실행 중이어야 한다.
REST(회원가입→검증→계획→통화생성) + WebSocket(실시간 통화) 전 흐름을 검증한다.
"""
import base64
import json
import os
import socket
import time
import urllib.request
import uuid

BASE = "http://localhost:8000"


def http(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class WsClient:
    """버퍼링 WebSocket 클라이언트 (recv 로 받은 여분 바이트를 보관)."""

    def __init__(self, s):
        self.s = s
        self.buf = b""

    def _fill(self, n, timeout):
        self.s.settimeout(timeout)
        while len(self.buf) < n:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk

    def _take(self, n, timeout):
        self._fill(n, timeout)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_json(self, timeout=8):
        try:
            b0 = self._take(1, timeout)[0]
            b1 = self._take(1, timeout)[0]
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = int.from_bytes(self._take(2, timeout), "big")
            elif length == 127:
                length = int.from_bytes(self._take(8, timeout), "big")
            payload = self._take(length, timeout)
            if opcode == 0x8:
                return None
            return json.loads(payload.decode())
        except (socket.timeout, TimeoutError, ConnectionError):
            return "__timeout__"

    def send(self, obj):
        ws_send(self.s, obj)

    def close(self):
        self.s.close()


def ws_connect(path):
    host, port = "localhost", 8000
    s = socket.create_connection((host, port))
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())
    client = WsClient(s)
    # 핸드셰이크 응답 헤더(\r\n\r\n)까지만 소비, 나머지는 buf 에 보관
    client._fill(4, 8)
    while b"\r\n\r\n" not in client.buf:
        client._fill(len(client.buf) + 1, 8)
    idx = client.buf.index(b"\r\n\r\n") + 4
    head, client.buf = client.buf[:idx], client.buf[idx:]
    assert b"101" in head, f"handshake failed: {head!r}"
    return client


def ws_send(s, obj):
    payload = json.dumps(obj).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += n.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += n.to_bytes(8, "big")
    s.sendall(bytes(header) + mask + masked)


def ws_recv(s, timeout=5):
    s.settimeout(timeout)
    b0 = s.recv(1)
    if not b0:
        return None
    b1 = s.recv(1)[0]
    length = b1 & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(s, 2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(s, 8), "big")
    payload = _recv_exact(s, length)
    opcode = b0[0] & 0x0F
    if opcode == 0x8:
        return None  # close
    return json.loads(payload.decode())


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def run():
    passed = []
    failed = []

    def check(name, cond):
        (passed if cond else failed).append(name)
        print(("PASS " if cond else "FAIL ") + name)

    email = f"test_{uuid.uuid4().hex[:8]}@example.com"

    # 1. 회원가입
    st, r = http("POST", "/api/register",
                 {"email": email, "password": "secret123", "display_name": "테스터"})
    check("회원가입 성공", st == 200 and "token" in r)
    token = r.get("token")

    # 2. 미인증 발신 차단
    st, _ = http("POST", "/api/calls", {"phone": "x", "plan": {}, "mode": "auto"})
    check("미인증 발신 차단(401)", st == 401)

    # 3. 정보 부족 → 추가 질문
    st, r = http("POST", "/api/analyze",
                 {"instruction": "내일 병원 예약 좀 해줘", "slots": {}}, token=token)
    check("목적 자동감지=hospital", r.get("purpose") == "hospital")
    check("정보 부족 판단", r.get("sufficient") is False and r.get("question"))

    # 4. 슬롯 채우기 → 충분
    slots = {"place": "OO내과", "department": "내과", "date": "내일", "time": "오전"}
    st, r = http("POST", "/api/analyze",
                 {"instruction": "내일 병원 예약", "purpose": "hospital", "slots": slots}, token=token)
    check("정보 충분 판단", r.get("sufficient") is True)

    # 5. 계획 생성
    st, r = http("POST", "/api/plan",
                 {"purpose": "hospital", "instruction": "내일 병원 예약", "slots": slots}, token=token)
    plan = r.get("plan")
    check("통화 계획 생성", st == 200 and plan and plan.get("goal"))

    # 6. 통화 생성
    st, r = http("POST", "/api/calls",
                 {"phone": "051-123-4567", "plan": plan, "mode": "assisted"}, token=token)
    call_id = r.get("call_id")
    check("통화 레코드 생성", st == 200 and call_id)

    # 7. WebSocket 실시간 통화
    client = ws_connect(f"/ws/call?token={token}&call_id={call_id}")
    events = []
    got_decision = False
    ended = None
    deadline = time.time() + 20
    decided = False
    while time.time() < deadline:
        msg = client.recv_json(timeout=8)
        if msg == "__timeout__":
            continue
        if msg is None:
            break
        if msg["type"] == "event":
            ev = msg["event"]
            events.append(ev)
            if ev.get("kind") == "decision" and not decided:
                got_decision = True
                client.send({"type": "decision", "choice": ev["options"][0]})
                decided = True
        elif msg["type"] == "ended":
            ended = msg["result"]
            break
    client.close()

    ai_opening = any("AI 통화 도우미" in e.get("text", "") for e in events if e["role"] == "ai")
    check("AI 고지 멘트 포함", ai_opening)
    check("상대방 발화 자막 수신", any(e["role"] == "other" for e in events))
    check("사용자 결정 요청 수신", got_decision)
    check("통화 종료 및 요약", ended is not None and ended.get("summary"))
    check("목적 달성 판정 존재", ended and ended.get("outcome") in ("success", "failed", "needs_action"))

    # 8. 기록 저장 확인
    st, r = http("GET", "/api/calls", token=token)
    ended_calls = [c for c in r.get("calls", []) if c["status"] == "ended"]
    check("통화 기록 저장됨", len(ended_calls) >= 1)

    # 9. 예약 생성 + 발신 검증(스케줄러)
    future = time.time() + 2
    st, r = http("POST", "/api/reservations",
                 {"phone": "051-999-0000", "plan": plan, "scheduled_at": future}, token=token)
    res_id = r.get("reservation_id")
    check("예약 생성", st == 200 and res_id)

    # 스케줄러가 처리할 때까지 대기
    done = False
    for _ in range(10):
        time.sleep(1)
        st, r = http("GET", "/api/reservations", token=token)
        target = next((x for x in r["reservations"] if x["id"] == res_id), None)
        if target and target["status"] in ("done", "failed"):
            done = True
            break
    check("예약 자동 발신 처리", done)

    # 10. 예약 전 정보부족 거부
    st, r = http("POST", "/api/reservations",
                 {"phone": "x", "plan": {"purpose": "hospital", "slots": {}, "instruction": ""},
                  "scheduled_at": time.time() + 60}, token=token)
    check("예약 전 정보부족 거부(400)", st == 400)

    print(f"\n결과: {len(passed)} PASS / {len(failed)} FAIL")
    if failed:
        print("실패:", failed)
    return len(failed) == 0


if __name__ == "__main__":
    ok = run()
    raise SystemExit(0 if ok else 1)
