"""REST API 핸들러. (경로 → 함수) 매핑으로 server.py 에서 호출된다.

각 핸들러는 (user, body, query) 를 받아 (status_code, dict) 를 반환한다.
user 가 None 이면 미인증.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ai_engine, auth, db

# ElevenLabs 실제 음성 통화 기본 에이전트 (키는 서버에서만 관리, NFR-005)
DEFAULT_AGENT_ID = "agent_9601m5193gryfwmajbcaxnxfjadd"


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def _require(user):
    if not user:
        raise ApiError(401, "로그인이 필요합니다.")


# ---------- Auth ----------
def register(user, body, query):
    uid, err = auth.register(body.get("email"), body.get("password"), body.get("display_name"))
    if err:
        return 400, {"error": err}
    token, _ = auth.login(body.get("email"), body.get("password"))
    u = auth.user_from_token(token)
    return 200, {"token": token, "user": _public_user(u)}


def login(user, body, query):
    token, err = auth.login(body.get("email"), body.get("password"))
    if err:
        return 401, {"error": err}
    u = auth.user_from_token(token)
    return 200, {"token": token, "user": _public_user(u)}


def logout(user, body, query):
    # 토큰은 서버에서 Authorization 헤더로 처리되므로 server.py 가 넘겨준 토큰 사용
    return 200, {"ok": True}


def me(user, body, query):
    _require(user)
    return 200, {"user": _public_user(user)}


def update_accessibility(user, body, query):
    _require(user)
    settings = body.get("accessibility", {})
    db.update_accessibility(user["id"], settings)
    return 200, {"ok": True, "accessibility": settings}


def _public_user(u):
    if not u:
        return None
    acc = {}
    try:
        acc = json.loads(u.get("accessibility") or "{}")
    except (ValueError, TypeError):
        acc = {}
    return {
        "id": u["id"],
        "email": u["email"],
        "display_name": u["display_name"],
        "accessibility": acc,
    }


# ---------- 프롬프트 검증 ----------
def analyze(user, body, query):
    """정보 충분성 검증 + 다음 질문 (FR-009, FR-010)."""
    _require(user)
    instruction = body.get("instruction", "")
    purpose = body.get("purpose") or ai_engine.detect_purpose(instruction) or "general"
    slots = body.get("slots", {})
    result = ai_engine.analyze_sufficiency(purpose, instruction, slots)
    return 200, result


def plan(user, body, query):
    """통화 계획 생성 (FR-011, FR-012)."""
    _require(user)
    purpose = body.get("purpose") or "general"
    instruction = body.get("instruction", "")
    slots = body.get("slots", {})
    p = ai_engine.build_plan(purpose, instruction, slots)
    return 200, {"plan": p}


# ---------- 통화 ----------
def create_call(user, body, query):
    """통화 승인 후 call 레코드 생성 (FR-015). WebSocket 에서 진행."""
    _require(user)
    p = body.get("plan", {})
    mode = body.get("mode", "assisted")
    call_id = db.create_call(user["id"], body.get("phone", ""), p.get("purpose", ""), p, mode)
    return 200, {"call_id": call_id}


def voice_config(user, body, query):
    """실제 음성 통화(ElevenLabs) 연결 정보.

    ELEVENLABS_API_KEY 가 있으면 서버에서 signed URL 을 발급해 키를 브라우저에 노출하지 않는다.
    없으면 agent_id 만 내려준다 (에이전트가 공개 설정일 때 동작).
    """
    _require(user)
    agent_id = os.environ.get("ELEVENLABS_AGENT_ID", "").strip() or DEFAULT_AGENT_ID
    out = {"agent_id": agent_id}
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        return 200, out
    url = ("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?"
           + urllib.parse.urlencode({"agent_id": agent_id}))
    req = urllib.request.Request(url, headers={"xi-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            out["signed_url"] = json.loads(res.read().decode("utf-8"))["signed_url"]
    except (urllib.error.URLError, ValueError, KeyError) as e:
        print(f"[voice] signed URL 발급 실패, agent_id 로 연결 시도: {e}")
    return 200, out


def _el_request(method, path, body=None):
    """ElevenLabs REST 호출 (서버 전용 키 사용)."""
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise ApiError(400, "실제 전화는 서버 .env 에 ELEVENLABS_API_KEY 가 필요합니다.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        "https://api.elevenlabs.io" + path, data=data, method=method,
        headers={"xi-api-key": api_key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print(f"[phone] ElevenLabs {method} {path} 실패 {e.code}: {detail}")
        raise ApiError(502, f"ElevenLabs 오류({e.code}): {detail}")
    except urllib.error.URLError as e:
        raise ApiError(502, f"ElevenLabs 연결 실패: {e.reason}")


def _to_e164(phone: str) -> str:
    """010-1234-5678 → +821012345678 (이미 + 로 시작하면 그대로)."""
    p = (phone or "").strip()
    digits = re.sub(r"\D", "", p)
    if p.startswith("+"):
        return "+" + digits
    if digits.startswith("0"):
        return "+82" + digits[1:]
    return "+" + digits


def _phone_number_id():
    pid = os.environ.get("ELEVENLABS_PHONE_NUMBER_ID", "").strip()
    if pid:
        return pid
    numbers = _el_request("GET", "/v1/convai/phone-numbers")
    if isinstance(numbers, dict):
        numbers = numbers.get("phone_numbers", [])
    if not numbers:
        raise ApiError(400, "ElevenLabs 에 연결된 전화번호가 없습니다.")
    return numbers[0]["phone_number_id"]


def phone_call_start(user, body, query):
    """실제 전화 발신: ElevenLabs 에이전트가 Twilio 번호로 상대방에게 전화를 건다."""
    _require(user)
    call = db.get_call(int(body.get("call_id", 0)))
    if not call or call["user_id"] != user["id"]:
        return 404, {"error": "통화를 찾을 수 없습니다."}
    plan_obj = _safe_json(call["plan"]) or {}
    request_text = " / ".join(x for x in [plan_obj.get("goal"), plan_obj.get("instruction")] if x)
    agent_id = os.environ.get("ELEVENLABS_AGENT_ID", "").strip() or DEFAULT_AGENT_ID
    res = _el_request("POST", "/v1/convai/twilio/outbound-call", {
        "agent_id": agent_id,
        "agent_phone_number_id": _phone_number_id(),
        "to_number": _to_e164(call["phone"]),
        "conversation_initiation_client_data": {"dynamic_variables": {
            "user_name": (body.get("my_name") or "").strip() or user.get("display_name") or "사용자",
            "ai_name": "춘식이",
            "call_type": ("가게·병원 같은 업체"
                          if plan_obj.get("purpose") in ("hospital", "restaurant", "store")
                          else "정해지지 않음 (요청 내용을 보고 업체인지 지인인지 판단)"),
            "phone_last4": (body.get("phone4") or "0000")[:4],
            "request": request_text or "예약 문의",
        }},
    })
    if not res.get("success", True) or not res.get("conversation_id"):
        return 502, {"error": res.get("message") or "전화 발신에 실패했습니다."}
    db.update_call(call["id"], status="in_call")
    return 200, {"conversation_id": res["conversation_id"], "message": res.get("message", "")}


def phone_call_status(user, body, query):
    """진행 중인 실제 전화의 대화 기록 (클라이언트가 2초마다 조회 → 실시간 자막)."""
    _require(user)
    cid = (query.get("id") or [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", cid or ""):
        return 400, {"error": "잘못된 통화 ID 입니다."}
    conv = _el_request("GET", f"/v1/convai/conversations/{cid}")
    transcript = [
        {"role": "ai" if t.get("role") == "agent" else "other",
         "text": t.get("message"), "kind": "speech"}
        for t in conv.get("transcript") or [] if t.get("message")
    ]
    return 200, {"status": conv.get("status"), "transcript": transcript}


_KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8,
           "아홉": 9, "열": 10, "열한": 11, "열두": 12, "일": 1, "이": 2, "삼": 3, "사": 4,
           "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9, "십": 10, "십일": 11, "십이": 12}
_NUM_WORD = r"\d{1,2}|열한|열두|열|한|두|세|네|다섯|여섯|일곱|여덟|아홉"
_TIME_RE = re.compile(
    rf"(오전|오후|아침|저녁|밤|낮)?\s*({_NUM_WORD})\s*시(\s*반|\s*\d{{1,2}}\s*분)?")
_DATE_RE = re.compile(
    r"(오늘|내일|모레|이번\s*주\s*\S요일|다음\s*주\s*\S요일|[월화수목금토일]요일|\d{1,2}월\s*\d{1,2}일)")
_PARTY_RE = re.compile(rf"({_NUM_WORD})\s*(명|분)(?!\s*(뒤|후|전|만))")


def _num(word):
    return int(word) if word.isdigit() else _KO_NUM.get(word)


def _confirmed_from_transcript(transcript):
    """통화 기록에서 마지막으로 확정된 날짜·시간·인원을 뽑는다.

    AI가 마무리 때 복창한 내용이 가장 정확하므로 뒤에서부터 AI 발화를 먼저 보고,
    없으면 상대방 발화까지 본다.
    """
    lines = [t for t in transcript if t.get("kind") == "speech" and t.get("text")]
    ordered = ([t for t in reversed(lines) if t.get("role") == "ai"]
               + [t for t in reversed(lines) if t.get("role") == "other"])
    found = {}
    for t in ordered:
        text = t["text"]
        if "time" not in found:
            m = _TIME_RE.search(text)
            if m and _num(m.group(2)):
                half = "반" if m.group(3) and "반" in m.group(3) else (m.group(3) or "").strip()
                found["time"] = " ".join(x for x in [m.group(1), f"{_num(m.group(2))}시{half}"] if x)
        if "date" not in found:
            m = _DATE_RE.search(text)
            if m:
                found["date"] = re.sub(r"\s+", " ", m.group(1))
        if "party_size" not in found:
            m = _PARTY_RE.search(text)
            if m and _num(m.group(1)):
                found["party_size"] = f"{_num(m.group(1))}명"
        if len(found) == 3:
            break
    return found


_SUCCESS_RE = re.compile(r"예약(이|은)?\s*(완료|확정)|잡아\s*드렸|잡아\s*드릴게|예약\s*됐|예약\s*되었")


def finish_call(user, body, query):
    """실제 음성 통화 종료 후 자막 기록 저장 + 요약 (FR-038 ~ FR-040)."""
    _require(user)
    call_id = int(body.get("call_id", 0))
    call = db.get_call(call_id)
    if not call or call["user_id"] != user["id"]:
        return 404, {"error": "통화를 찾을 수 없습니다."}
    transcript = body.get("transcript") or []
    plan_obj = _safe_json(call["plan"]) or {}

    # 대화에 예약 확정 표현이 있으면 달성, 상대방 발화가 없으면 미달성, 그 외는 확인 필요
    if any(_SUCCESS_RE.search(t.get("text", "")) for t in transcript):
        outcome = "success"
    elif not any(t.get("role") == "other" for t in transcript):
        outcome = "failed"
    else:
        outcome = "needs_action"

    slots = plan_obj.get("slots", {})
    confirmed = _confirmed_from_transcript(transcript)
    # 통화 중에 바뀐 날짜·시간·인원(예: 오전 → 오후 2시)이 있으면 그 값을 우선한다
    extracted = {k: v for k, v in {
        "장소": slots.get("place", ""),
        "날짜": confirmed.get("date") or slots.get("date", ""),
        "시간": confirmed.get("time") or slots.get("time", ""),
        "인원": confirmed.get("party_size") or slots.get("party_size", ""),
    }.items() if v}
    result = ai_engine.summarize(plan_obj, transcript, outcome, extracted)
    db.update_call(
        call_id,
        transcript=transcript,
        summary=result["summary"],
        outcome=result["outcome"],
        extracted=extracted,
        status="ended",
        ended_at=time.time(),
    )
    return 200, {"result": result}


def list_calls(user, body, query):
    _require(user)
    calls = db.list_calls(user["id"])
    out = []
    for c in calls:
        out.append({
            "id": c["id"],
            "phone": c["phone"],
            "purpose": c["purpose"],
            "summary": c["summary"],
            "outcome": c["outcome"],
            "status": c["status"],
            "mode": c["mode"],
            "created_at": c["created_at"],
            "plan": _safe_json(c["plan"]),
            "extracted": _safe_json(c["extracted"]),
            "transcript": _safe_json(c["transcript"]),
        })
    return 200, {"calls": out}


def delete_call(user, body, query):
    _require(user)
    cid = int(query.get("id", [0])[0]) if isinstance(query.get("id"), list) else int(body.get("id", 0))
    db.delete_call(cid, user["id"])
    return 200, {"ok": True}


# ---------- 예약 ----------
def create_reservation(user, body, query):
    """발신 예약 (FR-031, FR-032). scheduled_at 은 epoch 초."""
    _require(user)
    p = body.get("plan", {})
    scheduled_at = float(body.get("scheduled_at", 0))
    if scheduled_at <= 0:
        return 400, {"error": "예약 시각이 올바르지 않습니다."}
    # 발신 전 정보 검증 (FR-033)
    res = ai_engine.analyze_sufficiency(p.get("purpose") or "general",
                                        p.get("instruction", ""), p.get("slots", {}))
    if not res["sufficient"]:
        return 400, {"error": "예약 전 통화 정보가 부족합니다.", "analyze": res}
    rid = db.create_reservation(user["id"], body.get("phone", ""), p.get("purpose", ""), p, scheduled_at)
    return 200, {"reservation_id": rid}


def list_reservations(user, body, query):
    _require(user)
    rows = db.list_reservations(user["id"])
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "phone": r["phone"],
            "purpose": r["purpose"],
            "scheduled_at": r["scheduled_at"],
            "status": r["status"],
            "call_id": r["call_id"],
            "plan": _safe_json(r["plan"]),
        })
    return 200, {"reservations": out}


def cancel_reservation(user, body, query):
    _require(user)
    rid = int(body.get("id", 0))
    r = db.get_reservation(rid)
    if not r or r["user_id"] != user["id"]:
        return 404, {"error": "예약을 찾을 수 없습니다."}
    if r["status"] not in ("scheduled",):
        return 400, {"error": "이미 처리된 예약은 취소할 수 없습니다."}
    db.update_reservation(rid, user_id=user["id"], status="canceled")
    return 200, {"ok": True}


def _safe_json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


# 경로 → (핸들러, 메서드)
ROUTES = {
    ("POST", "/api/login"): login,
    ("POST", "/api/logout"): logout,
    ("GET", "/api/me"): me,
    ("POST", "/api/accessibility"): update_accessibility,
    ("POST", "/api/analyze"): analyze,
    ("POST", "/api/plan"): plan,
    ("POST", "/api/calls"): create_call,
    ("GET", "/api/calls"): list_calls,
    ("POST", "/api/calls/delete"): delete_call,
    ("GET", "/api/voice/config"): voice_config,
    ("POST", "/api/calls/finish"): finish_call,
    ("POST", "/api/phone/start"): phone_call_start,
    ("GET", "/api/phone/status"): phone_call_status,
    ("POST", "/api/reservations"): create_reservation,
    ("GET", "/api/reservations"): list_reservations,
    ("POST", "/api/reservations/cancel"): cancel_reservation,
}
