"""AI 엔진 (규칙 기반, 교체 가능).

실제 서비스에서는 OpenAI Realtime / LLM 으로 교체할 수 있도록
입력(통화 목적·정보) → 출력(질문/계획/발화/요약) 인터페이스를 분리했다.

담당 기능:
- analyze_sufficiency: 정보 충분성 검증 + 동적 추가 질문 (FR-009, FR-010)
- build_plan: 통합 통화 계획 생성 (FR-011, FR-012)
- CallSimulator: 상대방 발화 시뮬레이션 + AI 응답 + 사용자 개입 반영
  (FR-017 ~ FR-019, FR-022, FR-026)
- summarize: 통화 요약 + 목적 달성 판정 + 정보 추출 (FR-038 ~ FR-040)
"""
from __future__ import annotations

import random
import re

# 지원하는 통화 목적 (단기 프로젝트 범위 제한: 병원/식당/매장)
PURPOSES = {
    "hospital": {
        "label": "병원 예약",
        "required": ["place", "department", "date", "time"],
        "questions": {
            "place": {"text": "어느 병원에 예약할까요?", "type": "text",
                      "placeholder": "예: OO내과의원"},
            "department": {"text": "진료과를 선택해 주세요.", "type": "choice",
                           "options": ["내과", "치과", "정형외과", "이비인후과", "기타"]},
            "date": {"text": "원하는 날짜가 있나요?", "type": "text",
                     "placeholder": "예: 내일, 10월 20일"},
            "time": {"text": "원하는 시간대가 있나요?", "type": "choice",
                     "options": ["오전", "오후", "상관없음"]},
        },
    },
    "restaurant": {
        "label": "식당 예약",
        "required": ["place", "date", "time", "party_size"],
        "questions": {
            "place": {"text": "어느 식당에 예약할까요?", "type": "text",
                      "placeholder": "예: OO식당"},
            "date": {"text": "예약 날짜가 언제인가요?", "type": "text",
                     "placeholder": "예: 이번 주 토요일"},
            "time": {"text": "몇 시로 예약할까요?", "type": "text",
                     "placeholder": "예: 저녁 7시"},
            "party_size": {"text": "몇 분이 방문하시나요?", "type": "choice",
                           "options": ["1명", "2명", "3~4명", "5명 이상"]},
        },
    },
    "store": {
        "label": "매장 문의",
        "required": ["place", "inquiry"],
        "questions": {
            "place": {"text": "어느 매장에 문의할까요?", "type": "text",
                      "placeholder": "예: OO전자 서면점"},
            "inquiry": {"text": "무엇을 문의할까요?", "type": "choice",
                        "options": ["재고 확인", "영업시간", "가격 문의", "기타"]},
        },
    },
}

# 위 3가지에 해당하지 않는 요청은 사용자가 쓴 지시사항 그대로 통화한다 (추가 질문 없음)
PURPOSES["general"] = {
    "label": "자유 요청",
    "required": [],
    "questions": {},
}

FIELD_LABELS = {
    "place": "장소", "department": "진료과", "date": "날짜", "time": "시간",
    "party_size": "인원", "inquiry": "문의 내용",
}


def detect_purpose(text: str) -> str | None:
    """자연어에서 통화 목적을 추정."""
    t = text or ""
    if re.search(r"병원|진료|내과|치과|예약.*병원|의원", t):
        return "hospital"
    # 분명한 단어가 있을 때만 분류하고, 애매하면 None → 자유 요청(쓴 내용 그대로 통화)
    if re.search(r"식당|레스토랑|맛집|회식", t):
        return "restaurant"
    if re.search(r"매장|재고|영업시간", t):
        return "store"
    return None


def _extract_slots(text: str, purpose: str, known: dict) -> dict:
    """자연어에서 알 수 있는 슬롯을 간단히 추출."""
    t = text or ""
    slots = dict(known)
    # 날짜
    m = re.search(r"(오늘|내일|모레|이번 주 \w요일|다음 주 \w요일|\d{1,2}월\s*\d{1,2}일|\d{1,2}일)", t)
    if m and not slots.get("date"):
        slots["date"] = m.group(1)
    # 시간대
    if "오전" in t and not slots.get("time"):
        slots["time"] = "오전"
    elif "오후" in t and not slots.get("time"):
        slots["time"] = "오후"
    tm = re.search(r"(\d{1,2})\s*시", t)
    if tm and not slots.get("time"):
        slots["time"] = f"{tm.group(1)}시"
    # 진료과
    for dept in ["내과", "치과", "정형외과", "이비인후과"]:
        if dept in t and not slots.get("department"):
            slots["department"] = dept
    # 인원
    pm = re.search(r"(\d+)\s*명", t)
    if pm and not slots.get("party_size"):
        slots["party_size"] = f"{pm.group(1)}명"
    return slots


def analyze_sufficiency(purpose: str, instruction: str, slots: dict):
    """정보가 충분한지 판단하고, 부족하면 다음 질문 1개를 동적으로 생성.

    반환: {"sufficient": bool, "slots": {...}, "question": {...}|None, "missing": [...]}
    """
    if purpose not in PURPOSES:
        purpose = detect_purpose(instruction) or "general"
    spec = PURPOSES[purpose]
    slots = _extract_slots(instruction, purpose, slots or {})

    missing = [f for f in spec["required"] if not slots.get(f)]
    if not missing:
        return {"sufficient": True, "slots": slots, "question": None,
                "missing": [], "purpose": purpose}

    field = missing[0]
    q = dict(spec["questions"][field])
    q["field"] = field
    return {"sufficient": False, "slots": slots, "question": q,
            "missing": missing, "purpose": purpose}


def build_plan(purpose: str, instruction: str, slots: dict) -> dict:
    """사용자가 승인하기 전에 보여줄 통화 계획 (FR-012)."""
    if purpose not in PURPOSES:
        purpose = "general"
    spec = PURPOSES[purpose]
    goal_parts = []
    if purpose == "hospital":
        goal_parts.append(f"{slots.get('place','병원')}에 {slots.get('department','')} 진료 예약")
        goal_parts.append(f"{slots.get('date','')} {slots.get('time','')} 희망")
    elif purpose == "restaurant":
        goal_parts.append(f"{slots.get('place','식당')} 예약")
        goal_parts.append(f"{slots.get('date','')} {slots.get('time','')}, {slots.get('party_size','')}")
    elif purpose == "store":
        goal_parts.append(f"{slots.get('place','매장')}에 {slots.get('inquiry','문의')}")
    elif purpose == "general":
        goal_parts.append(instruction or "요청하신 내용 전달")
    goal = " · ".join(p.strip() for p in goal_parts if p.strip())

    return {
        "purpose": purpose,
        "purpose_label": spec["label"],
        "goal": goal,
        "slots": slots,
        "instruction": instruction,
        "constraints": [
            "확실하지 않은 정보는 임의로 답하지 않는다.",
            "결제·계약·개인정보 제공 등 중요 결정은 사용자 승인을 받는다.",
        ],
    }


# ---------- 통화 시뮬레이션 ----------

class CallSimulator:
    """상대방 발화와 AI 응답을 턴 기반으로 시뮬레이션한다.

    실제로는 Twilio 음성 스트림 + OpenAI Realtime 이 담당할 부분을,
    1단계 프로토타입에서는 결정적 시나리오로 대체한다.
    각 step()은 (events) 리스트를 반환한다. event = {role, text, kind}.
    role: ai | other | system | decision
    """

    def __init__(self, plan: dict):
        self.plan = plan
        self.purpose = plan.get("purpose") or "general"
        self.slots = plan.get("slots", {})
        self.turn = 0
        self.done = False
        self.outcome = None  # success / needs_action / failed
        self.extracted = {}
        self.pending_decision = None  # 사용자 승인 대기 중인 결정
        self._script = self._build_script()

    # 안내 멘트 (FR-016)
    def opening(self):
        return (
            "안녕하세요. 저는 청각 및 음성 의사소통 지원을 위해 사용자 대신 "
            "전화드리는 AI 통화 도우미입니다. 사용자의 요청에 따라 통화를 "
            "진행하고 있습니다. 통화가 괜찮으실까요?"
        )

    def _build_script(self):
        """목적별 상대방 발화 시나리오."""
        if self.purpose == "hospital":
            place = self.slots.get("place", "병원")
            dept = self.slots.get("department", "")
            date = self.slots.get("date", "요청하신 날짜")
            time = self.slots.get("time", "")
            return [
                {"other": "네, 괜찮습니다. 말씀하세요."},
                {"other": f"네 {place}입니다. 어떤 예약을 도와드릴까요?"},
                {"ai": f"{date} {time}에 {dept} 진료 예약을 하고 싶습니다. 가능할까요?"},
                {"other": f"{time if time else '해당 시간'}은 예약이 마감됐고, 바로 다음 시간대는 가능합니다.",
                 "decision": {
                     "prompt": "요청한 시간대가 마감됐어요. 바로 다음 시간대로 예약할까요?",
                     "options": ["다음 시간대로 예약", "다른 시간 문의", "예약하지 않음"],
                 }},
            ]
        if self.purpose == "restaurant":
            place = self.slots.get("place", "식당")
            party = self.slots.get("party_size", "")
            date = self.slots.get("date", "요청하신 날짜")
            time = self.slots.get("time", "")
            return [
                {"other": "네, 괜찮습니다."},
                {"other": f"{place}입니다. 예약 도와드릴까요?"},
                {"ai": f"{date} {time}에 {party} 예약하고 싶습니다."},
                {"other": "그 시간은 자리가 거의 찼는데, 창가 자리 하나는 가능합니다.",
                 "decision": {
                     "prompt": "창가 자리 하나만 가능하다고 해요. 예약할까요?",
                     "options": ["창가 자리로 예약", "다른 시간 문의", "예약하지 않음"],
                 }},
            ]
        if self.purpose == "general":
            request = self.plan.get("instruction") or "요청하신 내용"
            return [
                {"other": "네, 괜찮습니다. 말씀하세요."},
                {"ai": f"{request} 관련해서 연락드렸어요. 가능할까요?"},
                {"other": "네, 확인해 보니 가능합니다. 그렇게 진행해 드릴게요."},
            ]
        # store
        place = self.slots.get("place", "매장")
        inquiry = self.slots.get("inquiry", "문의")
        return [
            {"other": "네, 괜찮습니다."},
            {"other": f"{place}입니다. 무엇을 도와드릴까요?"},
            {"ai": f"{inquiry} 때문에 연락드렸습니다. 확인 부탁드립니다."},
            {"other": "네, 확인해 드리겠습니다. 재고 있고 영업은 오후 9시까지입니다."},
        ]

    def start(self):
        """통화 시작: 안내 멘트 + 상대방 첫 응답."""
        events = [
            {"role": "system", "text": f"{self.plan.get('purpose_label','통화')} 연결됨", "kind": "status"},
            {"role": "ai", "text": self.opening(), "kind": "speech"},
        ]
        # 상대방 첫 수락
        events.append({"role": "other", "text": self._script[0]["other"], "kind": "speech"})
        self.turn = 1
        return events

    def step(self, user_instruction: str | None = None):
        """한 턴 진행. user_instruction 이 있으면 현재 맥락에 반영 (FR-026)."""
        if self.done:
            return []
        events = []

        if user_instruction:
            events.append({
                "role": "ai",
                "text": f"(사용자 지시 반영) {user_instruction}",
                "kind": "speech",
            })

        if self.turn >= len(self._script):
            return self._finish(events)

        node = self._script[self.turn]
        if "ai" in node:
            events.append({"role": "ai", "text": node["ai"], "kind": "speech"})
        if "other" in node:
            events.append({"role": "other", "text": node["other"], "kind": "speech"})
        if "decision" in node:
            self.pending_decision = node["decision"]
            events.append({
                "role": "decision",
                "text": node["decision"]["prompt"],
                "options": node["decision"]["options"],
                "kind": "decision",
            })
            self.turn += 1
            return events  # 사용자 결정 대기

        self.turn += 1
        if self.turn >= len(self._script):
            return events + self._finish([])
        return events

    def resolve_decision(self, choice: str):
        """사용자가 결정을 내렸을 때 (FR-013, 승인 기반 중요 결정)."""
        self.pending_decision = None
        events = [{"role": "system", "text": f"사용자 선택: {choice}", "kind": "status"}]
        if "예약하지 않음" in choice:
            self.outcome = "failed"
            events.append({"role": "ai", "text": "확인했습니다. 이번에는 예약하지 않겠습니다. 감사합니다.", "kind": "speech"})
            return events + self._finish([])
        if "다른 시간" in choice:
            events.append({"role": "ai", "text": "그럼 다른 가능한 시간이 있는지 확인 부탁드립니다.", "kind": "speech"})
            events.append({"role": "other", "text": "그 다음 시간대는 자리가 있습니다. 그걸로 잡아드릴까요?", "kind": "speech"})
            self.pending_decision = {
                "prompt": "다음으로 가능한 시간이 있다고 해요. 예약할까요?",
                "options": ["예약 확정", "예약하지 않음"],
            }
            events.append({"role": "decision", "text": self.pending_decision["prompt"],
                           "options": self.pending_decision["options"], "kind": "decision"})
            return events
        # 예약 확정 계열
        self.outcome = "success"
        self._fill_extracted(confirmed_time=True)
        events.append({"role": "ai", "text": "네, 그 시간으로 예약 부탁드립니다.", "kind": "speech"})
        events.append({"role": "other", "text": "예약 완료되었습니다. 성함과 연락처만 확인하겠습니다.", "kind": "speech"})
        events.append({"role": "ai", "text": "확인해 주셔서 감사합니다. 예약 잘 받았습니다.", "kind": "speech"})
        return events + self._finish([])

    def _fill_extracted(self, confirmed_time=False):
        self.extracted = {
            "장소": self.slots.get("place", ""),
            "날짜": self.slots.get("date", ""),
            "시간": self.slots.get("time", "") + (" 이후 가능 시간" if confirmed_time else ""),
        }
        if self.purpose == "hospital":
            self.extracted["진료과"] = self.slots.get("department", "")
        if self.purpose == "restaurant":
            self.extracted["인원"] = self.slots.get("party_size", "")
        if self.purpose == "store":
            self.extracted["문의"] = self.slots.get("inquiry", "")

    def _finish(self, events):
        self.done = True
        if self.outcome is None:
            # store 문의처럼 결정 없이 끝나는 경우
            self.outcome = "success"
            self._fill_extracted()
        events.append({"role": "ai", "text": "도움 주셔서 감사합니다. 좋은 하루 되세요.", "kind": "speech"})
        events.append({"role": "other", "text": "네, 감사합니다. 안녕히 계세요.", "kind": "speech"})
        events.append({"role": "system", "text": "통화 종료", "kind": "status"})
        return events


def summarize(plan: dict, transcript: list, outcome: str, extracted: dict) -> dict:
    """통화 요약 + 목적 달성 판정 (FR-038 ~ FR-040)."""
    label = plan.get("purpose_label", "통화")
    outcome_text = {
        "success": "목적 달성",
        "needs_action": "추가 조치 필요",
        "failed": "미달성",
    }.get(outcome, "확인 필요")

    lines = [f"[{label}] 결과: {outcome_text}"]
    if extracted:
        for k, v in extracted.items():
            if v:
                lines.append(f"- {k}: {v}")
    other_turns = sum(1 for t in transcript if t.get("role") == "other")
    lines.append(f"- 상대방 발화 {other_turns}회를 포함한 대화 완료")
    return {
        "summary": "\n".join(lines),
        "outcome": outcome,
        "outcome_text": outcome_text,
        "extracted": extracted,
    }
