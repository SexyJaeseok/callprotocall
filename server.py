"""CallProtocol 1단계 프로토타입 서버.

표준 라이브러리만으로:
- 정적 파일 서빙 (web/)
- REST API (app/api.py)
- WebSocket 실시간 통화 세션 (app/websocket.py)
- 예약 발신 스케줄러 (백그라운드 스레드)

실행:  python server.py   →  http://localhost:8000
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from app import ai_engine, api, auth, db
from app.websocket import WebSocketConnection, compute_accept

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
HOST = "0.0.0.0"
PORT = 8000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 조용히

    # ---------- 공통 ----------
    def _token(self):
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        return None

    def _send_json(self, status, obj):
        # 기록에 깨진 문자(잘못 인코딩된 한글 등)가 있어도 응답 전체가 실패하지 않도록 대체 문자로 바꾼다
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------- 라우팅 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # WebSocket 업그레이드
        if path == "/ws/call" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws(parse_qs(parsed.query))
            return

        if path.startswith("/api/"):
            self._handle_api("GET", path, parse_qs(parsed.query))
            return

        self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api("POST", parsed.path, parse_qs(parsed.query))
            return
        self._send_json(404, {"error": "not found"})

    def _handle_api(self, method, path, query):
        handler = api.ROUTES.get((method, path))
        if not handler:
            self._send_json(404, {"error": "경로를 찾을 수 없습니다."})
            return
        try:
            body = self._read_body() if method == "POST" else {}
            token = self._token()
            user = auth.user_from_token(token)
            # logout 특수 처리
            if path == "/api/logout":
                auth.logout(token)
            status, result = handler(user, body, query)
            self._send_json(status, result)
        except api.ApiError as e:
            self._send_json(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._send_json(500, {"error": "서버 오류가 발생했습니다."})

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        # 경로 조작 방지
        safe = os.path.normpath(path).lstrip("\\/")
        full = os.path.join(WEB_DIR, safe)
        if not os.path.abspath(full).startswith(os.path.abspath(WEB_DIR)):
            self._send_json(403, {"error": "forbidden"})
            return
        if not os.path.isfile(full):
            # SPA 폴백
            full = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(full):
                self._send_json(404, {"error": "not found"})
                return
        ext = os.path.splitext(full)[1]
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------- WebSocket 통화 세션 ----------
    def _handle_ws(self, query):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json(400, {"error": "bad ws request"})
            return
        # 인증 (쿼리 토큰)
        token = (query.get("token") or [None])[0]
        user = auth.user_from_token(token)
        call_id = (query.get("call_id") or [None])[0]
        if not user or not call_id:
            self.send_response(401)
            self.end_headers()
            return

        accept = compute_accept(key)
        # 수동 핸드셰이크: send_response 가 추가하는 Server/Date 헤더를 피하고
        # 버퍼를 즉시 flush 해 클라이언트가 프레임보다 먼저 101 을 받도록 한다.
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        self.wfile.write(handshake.encode("ascii"))
        self.wfile.flush()
        # 이후 프레임은 raw 소켓으로 직접 전송
        self.close_connection = True  # 세션 종료 후 keep-alive 루프 중단

        ws = WebSocketConnection(self.connection, rfile=self.rfile, wfile=self.wfile)
        try:
            run_call_session(ws, user, int(call_id))
        except Exception:
            traceback.print_exc()
        finally:
            ws.close()


def run_call_session(ws: WebSocketConnection, user, call_id: int):
    """WebSocket 기반 실시간 통화 세션 (FR-023 ~ FR-029)."""
    call = db.get_call(call_id)
    if not call or call["user_id"] != user["id"]:
        ws.send(json.dumps({"type": "error", "message": "통화를 찾을 수 없습니다."}))
        return

    plan = json.loads(call["plan"]) if call["plan"] else {}
    sim = ai_engine.CallSimulator(plan)
    transcript = []

    def emit(events):
        for ev in events:
            transcript.append(ev)
            ws.send(json.dumps({"type": "event", "event": ev}, ensure_ascii=False))
            time.sleep(0.6)  # 자연스러운 대화 속도

    def advance():
        """결정 대기 또는 통화 종료 전까지 AI 대화를 자동 진행."""
        guard = 0
        while not sim.done and not sim.pending_decision and guard < 20:
            evs = sim.step()
            guard += 1
            if not evs:
                break
            emit(evs)

    db.update_call(call_id, status="in_call")
    ws.send(json.dumps({"type": "started", "mode": call["mode"]}, ensure_ascii=False))

    # 통화 시작: 안내 멘트 + 상대방 수락
    emit(sim.start())
    # 사용자 개입이 필요한 지점(결정) 또는 종료까지 자동 진행
    advance()

    # 사용자 개입 루프
    while not sim.done:
        if sim.pending_decision:
            ws.send(json.dumps({"type": "await_decision"}, ensure_ascii=False))
        msg = ws.receive()
        if msg is None:
            break
        try:
            data = json.loads(msg)
        except ValueError:
            continue

        mtype = data.get("type")
        if mtype == "decision":
            events = sim.resolve_decision(data.get("choice", ""))
            emit(events)
            advance()
        elif mtype == "instruct":
            # 실시간 사용자 지시 (FR-025, FR-026)
            events = sim.step(user_instruction=data.get("text", ""))
            emit(events)
            advance()
        elif mtype == "say":
            # 텍스트 직접 통화 (FR-030): 사용자가 직접 전달
            text = data.get("text", "")
            ev = {"role": "ai", "text": f"(사용자 직접 전달) {text}", "kind": "speech"}
            transcript.append(ev)
            ws.send(json.dumps({"type": "event", "event": ev}, ensure_ascii=False))
            # 상대방 반응 자동 진행
            advance()
        elif mtype == "end":
            # 사용자가 통화 종료 (FR-028)
            sim.outcome = sim.outcome or "needs_action"
            sim.done = True
            ev = {"role": "system", "text": "사용자가 통화를 종료했습니다.", "kind": "status"}
            transcript.append(ev)
            ws.send(json.dumps({"type": "event", "event": ev}, ensure_ascii=False))
            break
        elif mtype == "continue":
            if not sim.pending_decision:
                emit(sim.step())

    # 요약 및 저장 (FR-038 ~ FR-040)
    result = ai_engine.summarize(plan, transcript, sim.outcome or "needs_action", sim.extracted)
    db.update_call(
        call_id,
        transcript=transcript,
        summary=result["summary"],
        outcome=result["outcome"],
        extracted=sim.extracted,
        status="ended",
        ended_at=time.time(),
    )
    ws.send(json.dumps({"type": "ended", "result": result}, ensure_ascii=False))


# ---------- 예약 스케줄러 ----------
def scheduler_loop():
    """예약 시각이 된 발신을 처리 (FR-032). 중복 방지는 db.due_reservations 에서 처리."""
    while True:
        try:
            now = time.time()
            due = db.due_reservations(now)
            for r in due:
                plan = json.loads(r["plan"]) if r["plan"] else {}
                call_id = db.create_call(r["user_id"], r["phone"], r["purpose"], plan, "auto")
                # 예약 통화는 자동 시뮬레이션으로 바로 완료 처리
                sim = ai_engine.CallSimulator(plan)
                transcript = sim.start()
                while not sim.done:
                    if sim.pending_decision:
                        # 사전 승인되지 않은 결정 → 안전하게 보류 (FR-037, 윤리정책)
                        transcript.append({
                            "role": "system",
                            "text": "사용자 승인이 필요한 결정이 있어 통화를 보류했습니다.",
                            "kind": "status",
                        })
                        sim.outcome = "needs_action"
                        sim.done = True
                        break
                    transcript.extend(sim.step())
                res = ai_engine.summarize(plan, transcript, sim.outcome or "needs_action", sim.extracted)
                db.update_call(call_id, transcript=transcript, summary=res["summary"],
                               outcome=res["outcome"], extracted=sim.extracted,
                               status="ended", ended_at=time.time())
                db.update_reservation(r["id"], status="done", call_id=call_id)
                print(f"[scheduler] 예약 #{r['id']} 발신 완료 → call #{call_id} ({res['outcome']})")
        except Exception:
            traceback.print_exc()
        time.sleep(2)


def load_dotenv(path=os.path.join(os.path.dirname(__file__), ".env")):
    """.env 의 KEY=VALUE 를 환경 변수로 읽는다 (이미 설정된 값은 유지)."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_dotenv()
    db.init_db()
    auth.ensure_seed_account()  # 고정 로그인 계정(dongeui) 보장
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CallProtocol 서버 실행 중 →  http://localhost:{PORT}")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
        server.shutdown()


if __name__ == "__main__":
    main()
