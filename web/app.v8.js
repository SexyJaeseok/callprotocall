// CallProtocol 프론트엔드 로직  (build v8: 새 디자인 + 실제 AI 음성 통화)

// JS 에러가 나면 화면에 바로 보여준다 (디버깅용)
window.addEventListener("error", (e) => {
  const box = document.getElementById("auth-error");
  if (box) box.textContent = "스크립트 오류: " + (e.message || e.error);
  console.error("global error:", e.error || e.message);
});

const State = {
  token: localStorage.getItem("cp_token") || null,
  user: null,
  mode: "login",       // login | register
  draft: {             // 현재 작성 중인 통화
    phone: "", purpose: "", instruction: "", slots: {}, mode: "assisted", plan: null,
  },
  ws: null,
  conv: null,          // ElevenLabs 실제 음성 통화 세션
  voiceTranscript: [],
  voiceFinished: false,
  callId: null,
  timer: null,
  timerStart: 0,
  reserveFromPlan: false,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---------- API ----------
async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (State.token) headers["Authorization"] = "Bearer " + State.token;
  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "요청 실패");
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2600);
}

// ---------- 인증 ----------
function initAuth() {
  $("#auth-form").addEventListener("submit", onAuthSubmit);
  $("#logout-btn").addEventListener("click", onLogout);
}

async function onAuthSubmit(e) {
  e.preventDefault();
  $("#auth-error").textContent = "";
  const email = $("#email").value.trim();
  const password = $("#password").value;
  try {
    const data = await api("/api/login", { method: "POST", body: { email, password } });
    State.token = data.token;
    State.user = data.user;
    localStorage.setItem("cp_token", data.token);
    enterApp();
  } catch (err) {
    $("#auth-error").textContent = err.message;
  }
}

async function onLogout() {
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  State.token = null;
  State.user = null;
  localStorage.removeItem("cp_token");
  $("#app-view").hidden = true;
  $("#auth-view").hidden = false;
}

// ---------- 앱 진입 ----------
function enterApp() {
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  $("#user-name").textContent = State.user?.display_name || State.user?.email || "";
  applyAccessibility(State.user?.accessibility || {});
  showPage("new");
  resetNewCall();
  updateAiBadge();
}

// 상단 배지: 지금 선택된 통화 엔진이 실제 AI 음성인지 시뮬레이션인지 표시
function updateAiBadge() {
  const badge = $("#ai-badge");
  if (!badge) return;
  const engine = $("#engine").value;
  if (engine === "sim") {
    badge.textContent = "시뮬레이션";
    badge.className = "ai-badge sim";
    badge.title = "인터넷 없이 대본으로 동작하는 백업 모드입니다.";
  } else {
    badge.textContent = "AI 실제 연동";
    badge.className = "ai-badge real";
    badge.title = engine === "phone" ? "실제 전화로 AI가 통화합니다." : "ElevenLabs 실제 AI 음성으로 통화합니다.";
  }
}

function showPage(page) {
  $$(".navbtn").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  ["new", "call", "result", "reservations", "history", "settings"].forEach((p) => {
    const el = $("#page-" + p);
    if (el) el.hidden = p !== page;
  });
  if (page === "reservations") loadReservations();
  if (page === "history") loadHistory();
}

function initNav() {
  $$(".navbtn").forEach((b) => b.addEventListener("click", () => {
    const page = b.dataset.page;
    if (page === "new") resetNewCall();
    showPage(page);
  }));
}

// ---------- 1) 프롬프트 입력 & AI 검토 ----------
function resetNewCall() {
  State.draft = {
    phone: "", purpose: "", instruction: "", slots: {}, mode: "assisted", plan: null,
    engine: $("#engine").value || "voice", phone4: $("#my-phone4").value.trim(),
    myName: $("#my-name").value.trim(),
  };
  $("#phone").value = "";
  $("#purpose").value = "";
  $("#instruction").value = "";
  const assisted = document.querySelector('input[name="mode"][value="assisted"]');
  if (assisted) assisted.checked = true;
  const adv = document.querySelector(".advanced");
  if (adv) adv.open = false;
  $("#step-input").hidden = false;
  $("#step-questions").hidden = true;
  $("#step-plan").hidden = true;
  $("#step-reserve").hidden = true;
}

function initNewCall() {
  $("#analyze-btn").addEventListener("click", startAnalyze);
  $("#engine").addEventListener("change", updateAiBadge);
  $("#plan-approve").addEventListener("click", approveAndCall);
  $("#plan-edit").addEventListener("click", () => {
    $("#step-plan").hidden = true;
    $("#step-input").hidden = false;
  });
  $("#plan-reserve").addEventListener("click", () => {
    $("#step-reserve").hidden = false;
    const dt = new Date(Date.now() + 60 * 1000);
    $("#reserve-time").value = toLocalInput(dt);
  });
  $("#reserve-cancel").addEventListener("click", () => ($("#step-reserve").hidden = true));
  $("#reserve-confirm").addEventListener("click", confirmReservation);
}

async function startAnalyze() {
  State.draft.phone = $("#phone").value.trim();
  State.draft.purpose = $("#purpose").value;
  State.draft.instruction = $("#instruction").value.trim();
  const checked = document.querySelector('input[name="mode"]:checked');
  State.draft.mode = checked ? checked.value : "assisted";
  State.draft.engine = $("#engine").value;
  State.draft.phone4 = $("#my-phone4").value.trim();
  State.draft.myName = $("#my-name").value.trim();
  State.draft.slots = {};
  if (!State.draft.phone) return toast("전화번호를 입력해 주세요.");
  if (!State.draft.instruction) return toast("통화 지시사항을 입력해 주세요.");
  await runAnalyzeStep();
}

async function runAnalyzeStep() {
  const data = await api("/api/analyze", {
    method: "POST",
    body: {
      purpose: State.draft.purpose,
      instruction: State.draft.instruction,
      slots: State.draft.slots,
    },
  });
  State.draft.purpose = data.purpose;
  State.draft.slots = data.slots;

  if (data.sufficient) {
    await buildPlan();
  } else {
    renderQuestion(data.question, data.missing);
  }
}

function renderQuestion(q, missing) {
  $("#step-input").hidden = true;
  $("#step-plan").hidden = true;
  $("#step-questions").hidden = false;
  $("#question-progress").textContent =
    `통화 목적에 맞게 ${missing.length}개 정보가 더 필요해요.`;
  const area = $("#question-area");
  area.innerHTML = "";

  const qt = document.createElement("p");
  qt.className = "q-text";
  qt.textContent = q.text;
  area.appendChild(qt);

  if (q.type === "choice") {
    const wrap = document.createElement("div");
    wrap.className = "q-options";
    q.options.forEach((opt) => {
      const btn = document.createElement("button");
      btn.className = "q-option";
      btn.textContent = opt;
      btn.addEventListener("click", () => answerQuestion(q.field, opt));
      wrap.appendChild(btn);
    });
    area.appendChild(wrap);
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = q.placeholder || "";
    input.id = "q-input";
    const btn = document.createElement("button");
    btn.className = "btn btn-primary";
    btn.textContent = "확인";
    btn.style.marginTop = "10px";
    const submit = () => {
      const val = input.value.trim();
      if (val) answerQuestion(q.field, val);
    };
    btn.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    area.appendChild(input);
    area.appendChild(btn);
    setTimeout(() => input.focus(), 50);
  }
}

async function answerQuestion(field, value) {
  State.draft.slots[field] = value;
  await runAnalyzeStep();
}

async function buildPlan() {
  const data = await api("/api/plan", {
    method: "POST",
    body: {
      purpose: State.draft.purpose,
      instruction: State.draft.instruction,
      slots: State.draft.slots,
    },
  });
  State.draft.plan = data.plan;
  renderPlan(data.plan);
}

function renderPlan(plan) {
  $("#step-questions").hidden = true;
  $("#step-input").hidden = true;
  $("#step-plan").hidden = false;
  const el = $("#plan-summary");
  const slotRows = Object.entries(plan.slots || {})
    .filter(([, v]) => v)
    .map(([k, v]) => `<div class="row"><span class="k">${labelFor(k)}</span><span>${esc(v)}</span></div>`)
    .join("");
  el.innerHTML = `
    <div class="goal">${esc(plan.purpose_label)}: ${esc(plan.goal || "통화 진행")}</div>
    ${slotRows}
    <div class="row"><span class="k">지시사항</span><span>${esc(plan.instruction || "-")}</span></div>
    <div class="row"><span class="k">전화번호</span><span>${esc(State.draft.phone)}</span></div>
    <div class="row"><span class="k">제어 모드</span><span>${modeLabel(State.draft.mode)}</span></div>
    <div class="row"><span class="k">통화 엔진</span><span>${engineLabel(State.draft.engine)}</span></div>
  `;
}

async function approveAndCall() {
  const data = await api("/api/calls", {
    method: "POST",
    body: { phone: State.draft.phone, plan: State.draft.plan, mode: State.draft.mode },
  });
  State.callId = data.call_id;
  if (State.draft.engine === "voice") startVoiceCall();
  else if (State.draft.engine === "phone") startPhoneCall();
  else startCall();
}

function prepareCallPage() {
  showPage("call");
  const plan = State.draft.plan;
  $("#call-title").textContent = plan.goal || plan.purpose_label;
  $("#call-sub").textContent = `${plan.purpose_label} · ${State.draft.phone}`;
  $("#mode-banner").textContent =
    modeLabel(State.draft.mode) + " 활성화 · " + engineLabel(State.draft.engine);
  $("#transcript").innerHTML = "";
  $("#decision-box").hidden = true;
  setConn(false, "연결 중...");
}

// ---------- 2-A) 실제 AI 음성 통화 (ElevenLabs) ----------
const ELEVENLABS_SDK = "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm";

// AI 비서가 통화에서 자기 소개할 때 쓰는 이름
const AI_NAME = "춘식이";

// 상대에 따라 말투를 바꾸도록 에이전트에 알려주는 통화 상대 종류
function callTypeText(purpose) {
  return ["hospital", "restaurant", "store"].includes(purpose)
    ? "가게·병원 같은 업체"
    : "정해지지 않음 (요청 내용을 보고 업체인지 지인인지 판단)";
}

function voiceRequestText(plan) {
  // 자유 요청은 goal 이 지시사항과 같으므로 중복을 뺀다
  const parts = [...new Set([plan.goal, plan.instruction].filter(Boolean))];
  return parts.join(" / ") || "예약 문의";
}

async function startVoiceCall() {
  prepareCallPage();
  State.voiceTranscript = [];
  State.voiceFinished = false;
  const plan = State.draft.plan;
  try {
    // 마이크 권한을 먼저 받는다 (localhost 또는 https 에서만 가능)
    await navigator.mediaDevices.getUserMedia({ audio: true });
    const [cfg, sdk] = await Promise.all([api("/api/voice/config"), import(ELEVENLABS_SDK)]);
    const session = cfg.signed_url ? { signedUrl: cfg.signed_url } : { agentId: cfg.agent_id };
    // 보이스톡 연결 시: 마이크 = 카톡 소리가 들어오는 가상 케이블, 스피커 = 카톡 마이크로 가는 가상 케이블
    const inputDeviceId = $("#in-device").value || undefined;
    const outputDeviceId = $("#out-device").value || undefined;
    State.conv = await sdk.Conversation.startSession({
      ...session,
      // WebRTC 연결은 outputDeviceId 를 무시하고 기본 스피커로만 재생하므로 WebSocket 을 쓴다
      connectionType: "websocket",
      inputDeviceId,
      outputDeviceId,
      dynamicVariables: {
        user_name: State.draft.myName || State.user?.display_name || "사용자",
        ai_name: AI_NAME,
        call_type: callTypeText(plan.purpose),
        phone_last4: State.draft.phone4 || "0000",
        request: voiceRequestText(plan),
      },
      onConnect: () => {
        setConn(true, "통화 중");
        startTimer();
        addVoiceEvent({ role: "system", text: `${plan.purpose_label} 연결됨 (실제 AI 음성)`, kind: "status" });
      },
      onMessage: ({ message, source }) => {
        // v3 음성 모델의 감정 태그([gentle] 등)는 자막에서 숨긴다
        const text = (message || "").replace(/\[[a-z ]+\]\s*/gi, "").trim();
        if (!text) return;
        if (source !== "ai") hideSpeakingIndicator();
        addVoiceEvent({ role: source === "ai" ? "ai" : "other", text, kind: "speech" });
      },
      // 음성 인식 결과는 문장이 끝나야 오므로, 말하기 시작하는 순간 바로 "말하는 중" 표시
      onVadScore: ({ vadScore }) => {
        if (vadScore > 0.6) showSpeakingIndicator();
      },
      onModeChange: ({ mode }) => {
        if (mode === "speaking") hideSpeakingIndicator();
        if (!State.voiceFinished) setConn(true, mode === "speaking" ? "AI 말하는 중" : "상대방 듣는 중");
      },
      onError: (message) => {
        console.error("voice error:", message);
        toast("음성 통화 오류: " + message);
        addVoiceEvent({ role: "system", text: "음성 통화 오류: " + message, kind: "status" });
      },
      onDisconnect: (details) => {
        // 크레딧 소진 등 서버 쪽 종료 사유를 자막에 남긴다
        const reason = details?.message || details?.closeReason || "";
        if (details?.reason && details.reason !== "user") {
          addVoiceEvent({ role: "system", text: `통화가 끊겼어요 (${details.reason}${reason ? ": " + reason : ""})`, kind: "status" });
        }
        finishVoiceCall();
      },
    });
  } catch (err) {
    console.error(err);
    if (/quota|credit/i.test(err.message || "")) {
      toast("ElevenLabs 크레딧이 소진됐어요 → 시뮬레이션으로 전환합니다.");
    } else {
      toast("실제 음성 연결 실패 → 시뮬레이션으로 전환합니다.");
    }
    addVoiceEvent({ role: "system", text: "음성 연결 실패: " + (err.message || err), kind: "status" });
    State.draft.engine = "sim";
    setTimeout(startCall, 1200);
  }
}

function addVoiceEvent(ev) {
  State.voiceTranscript.push(ev);
  renderEvent(ev);
  // 말하는 중 표시는 항상 자막 맨 아래에 둔다
  const ind = $("#speaking-indicator");
  if (ind) $("#transcript").appendChild(ind);
}

function showSpeakingIndicator() {
  if ($("#speaking-indicator")) return;
  const b = document.createElement("div");
  b.id = "speaking-indicator";
  b.className = "bubble other";
  b.innerHTML = '<div class="who">상대방</div><div>🎙 말하는 중…</div>';
  const t = $("#transcript");
  t.appendChild(b);
  t.scrollTop = t.scrollHeight;
}

function hideSpeakingIndicator() {
  $("#speaking-indicator")?.remove();
}

// 사용자 개입: 의뢰인 지시임을 표시한 메시지로 보내 AI가 즉시 상대방에게 반영하게 한다
// (sendContextualUpdate 는 상대방이 다시 말할 때까지 반영되지 않아 침묵이 생김)
function voiceIntervene(kind, text) {
  if (!State.conv) return;
  if (kind === "instruct") {
    State.conv.sendUserMessage(
      `[의뢰인 문자 지시 · 가게 직원의 말이 아님] ${text}. ` +
      "이 지시를 바로 반영해서 가게 직원에게 말해. 지시를 받았다는 말은 하지 마.");
    addVoiceEvent({ role: "system", text: `사용자 지시: ${text}`, kind: "status" });
  } else {
    State.conv.sendUserMessage(
      `[의뢰인 문자 지시 · 가게 직원의 말이 아님] 다음 문장을 바꾸지 말고 가게 직원에게 그대로 전달해: "${text}"`);
    addVoiceEvent({ role: "system", text: `직접 전달 요청: ${text}`, kind: "status" });
  }
}

async function finishVoiceCall() {
  if (State.voiceFinished) return;
  State.voiceFinished = true;
  State.conv = null;
  stopTimer();
  setConn(false, "통화 종료", true);
  try {
    const data = await api("/api/calls/finish", {
      method: "POST",
      body: { call_id: State.callId, transcript: State.voiceTranscript },
    });
    showResult(data.result);
  } catch (err) {
    toast("통화 기록 저장 실패: " + err.message);
  }
}

// 오디오 장치 목록 (장치 이름은 마이크 권한을 받은 뒤에만 보인다). 선택값은 브라우저에 기억.
async function loadAudioDevices({ silent = false } = {}) {
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
  } catch (_) {
    if (!silent) toast("마이크 권한이 필요해요.");
    return;
  }
  const devices = await navigator.mediaDevices.enumerateDevices();
  const fill = (sel, kind, prefix) => {
    const saved = localStorage.getItem("cp_" + sel.id) || "";
    sel.innerHTML = `<option value="">${prefix}: 기본 장치</option>`;
    devices.filter((d) => d.kind === kind && d.deviceId !== "default" && d.deviceId !== "communications")
      .forEach((d) => {
        const opt = document.createElement("option");
        opt.value = d.deviceId;
        opt.textContent = `${prefix}: ${d.label || d.deviceId.slice(0, 8)}`;
        sel.appendChild(opt);
      });
    sel.value = [...sel.options].some((o) => o.value === saved) ? saved : "";
  };
  fill($("#in-device"), "audioinput", "마이크");
  fill($("#out-device"), "audiooutput", "스피커");
  if (!silent) toast("장치 목록을 불러왔어요.");
}

function initDevicePicker() {
  $("#load-devices").addEventListener("click", () => loadAudioDevices());
  // 마이크 권한이 이미 있으면 새로고침해도 저장된 장치 선택을 자동으로 복원
  navigator.permissions?.query({ name: "microphone" })
    .then((p) => { if (p.state === "granted") loadAudioDevices({ silent: true }); })
    .catch(() => {});
  ["#in-device", "#out-device"].forEach((s) =>
    $(s).addEventListener("change", (e) => localStorage.setItem("cp_" + e.target.id, e.target.value)));
}

// ---------- 2-C) 실제 전화 (ElevenLabs + Twilio) ----------
// 통화는 전화망에서 진행되므로 끼어들기는 불가. 대화 기록을 2초마다 받아 자막으로 보여준다.
async function startPhoneCall() {
  prepareCallPage();
  State.voiceTranscript = [];
  State.voiceFinished = false;
  State.phoneShown = 0;
  try {
    const res = await api("/api/phone/start", {
      method: "POST",
      body: { call_id: State.callId, phone4: State.draft.phone4, my_name: State.draft.myName },
    });
    State.phoneConvId = res.conversation_id;
    setConn(true, "전화 거는 중");
    startTimer();
    addVoiceEvent({ role: "system", text: `실제 전화 발신 → ${State.draft.phone}`, kind: "status" });
    State.phonePoll = setInterval(pollPhoneCall, 2000);
  } catch (err) {
    toast("전화 발신 실패: " + err.message);
    addVoiceEvent({ role: "system", text: "전화 발신 실패: " + err.message, kind: "status" });
    setConn(false, "발신 실패", true);
  }
}

async function pollPhoneCall() {
  if (State.voiceFinished) return;
  let data;
  try {
    data = await api("/api/phone/status?id=" + encodeURIComponent(State.phoneConvId));
  } catch (_) { return; }  // 일시적 오류는 다음 조회에서 재시도
  const list = data.transcript || [];
  if (list.length) setConn(true, "통화 중");
  for (let i = State.phoneShown; i < list.length; i++) addVoiceEvent(list[i]);
  State.phoneShown = list.length;
  if (data.status === "done" || data.status === "failed") finishPhoneCall();
}

function finishPhoneCall() {
  clearInterval(State.phonePoll);
  State.phonePoll = null;
  State.phoneConvId = null;
  finishVoiceCall();
}

// ---------- 2-B) 시뮬레이션 통화 (백업, WebSocket) ----------
function startCall() {
  prepareCallPage();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/call?token=${encodeURIComponent(State.token)}&call_id=${State.callId}`;
  const ws = new WebSocket(url);
  State.ws = ws;

  ws.onopen = () => setConn(true, "통화 중");
  ws.onclose = () => { setConn(false, "연결 종료"); stopTimer(); };
  ws.onerror = () => toast("연결 오류가 발생했습니다.");
  ws.onmessage = (e) => handleWsMessage(JSON.parse(e.data));
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case "started":
      startTimer();
      break;
    case "event":
      renderEvent(msg.event);
      break;
    case "await_decision":
      // 결정 박스는 event(kind=decision)에서 이미 렌더링됨
      break;
    case "ended":
      stopTimer();
      setConn(false, "통화 종료", true);
      showResult(msg.result);
      break;
    case "error":
      toast(msg.message || "오류");
      break;
  }
}

function renderEvent(ev) {
  if (ev.kind === "decision") {
    showDecision(ev);
    return;
  }
  const t = $("#transcript");
  const bubble = document.createElement("div");
  const role = ev.role === "ai" ? "ai" : ev.role === "other" ? "other" : "system";
  bubble.className = `bubble ${role}`;
  if (State.highlightNew !== false && role !== "system") bubble.classList.add("new-highlight");

  if (role !== "system") {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = role === "ai" ? "AI 도우미" : "상대방";
    bubble.appendChild(who);
  }
  const text = document.createElement("div");
  text.textContent = ev.text;
  bubble.appendChild(text);
  t.appendChild(bubble);
  t.scrollTop = t.scrollHeight;
}

function showDecision(ev) {
  const box = $("#decision-box");
  box.hidden = false;
  $("#decision-prompt").textContent = ev.text;
  const opts = $("#decision-options");
  opts.innerHTML = "";
  (ev.options || []).forEach((opt) => {
    const btn = document.createElement("button");
    btn.className = "btn btn-secondary";
    btn.textContent = opt;
    btn.addEventListener("click", () => {
      box.hidden = true;
      wsSend({ type: "decision", choice: opt });
    });
    opts.appendChild(btn);
  });
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function wsSend(obj) {
  if (State.ws && State.ws.readyState === WebSocket.OPEN) {
    State.ws.send(JSON.stringify(obj));
  }
}

function initCallControls() {
  $("#instruct-btn").addEventListener("click", () => {
    const input = $("#intervene-input");
    const text = input.value.trim();
    if (!text) return;
    if (State.phoneConvId) return toast("실제 전화 모드에서는 끼어들기를 지원하지 않아요.");
    if (State.conv) voiceIntervene("instruct", text);
    else wsSend({ type: "instruct", text });
    input.value = "";
  });
  $("#say-btn").addEventListener("click", () => {
    const input = $("#intervene-input");
    const text = input.value.trim();
    if (!text) return;
    if (State.phoneConvId) return toast("실제 전화 모드에서는 끼어들기를 지원하지 않아요.");
    if (State.conv) voiceIntervene("say", text);
    else wsSend({ type: "say", text });
    input.value = "";
  });
  $("#intervene-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#instruct-btn").click();
  });
  $("#end-call-btn").addEventListener("click", () => {
    if (State.conv) {
      addVoiceEvent({ role: "system", text: "사용자가 통화를 종료했습니다.", kind: "status" });
      State.conv.endSession();  // onDisconnect → finishVoiceCall
    } else if (State.phoneConvId) {
      // 전화 자체는 상대방이 끊어야 종료됨. 여기서는 자막 수신만 멈추고 기록을 저장한다.
      addVoiceEvent({ role: "system", text: "자막 수신을 종료했습니다. (전화는 상대방 쪽에서 끊어 주세요)", kind: "status" });
      finishPhoneCall();
    } else {
      wsSend({ type: "end" });
    }
    toast("통화를 종료합니다.");
  });
  $("#result-done").addEventListener("click", () => { resetNewCall(); showPage("new"); });
}

// 통화 타이머
function startTimer() {
  State.timerStart = Date.now();
  stopTimer();
  State.timer = setInterval(() => {
    const s = Math.floor((Date.now() - State.timerStart) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    $("#call-timer").textContent = `${mm}:${ss}`;
  }, 500);
}
function stopTimer() { if (State.timer) { clearInterval(State.timer); State.timer = null; } }

function setConn(connected, text, ended = false) {
  const dot = $("#conn-dot");
  dot.classList.toggle("connected", connected);
  dot.classList.toggle("ended", ended);
  $("#conn-text").textContent = text;
}

// ---------- 3) 결과 ----------
function showResult(result) {
  showPage("result");
  const card = $("#result-card");
  const cls = "outcome-" + result.outcome;
  const extracted = result.extracted || {};
  const grid = Object.entries(extracted)
    .filter(([, v]) => v)
    .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`)
    .join("");
  card.innerHTML = `
    <span class="outcome-pill ${cls}">${esc(result.outcome_text)}</span>
    <div>${esc(result.summary)}</div>
    ${grid ? `<div class="extracted-grid">${grid}</div>` : ""}
  `;
}

// ---------- 예약 ----------
async function confirmReservation() {
  const val = $("#reserve-time").value;
  if (!val) return toast("발신 시각을 선택해 주세요.");
  const scheduled_at = new Date(val).getTime() / 1000;
  if (scheduled_at <= Date.now() / 1000 - 1) return toast("미래 시각을 선택해 주세요.");
  try {
    await api("/api/reservations", {
      method: "POST",
      body: { phone: State.draft.phone, plan: State.draft.plan, scheduled_at },
    });
    toast("예약이 등록되었습니다.");
    $("#step-reserve").hidden = true;
    resetNewCall();
    showPage("reservations");
  } catch (err) {
    toast(err.message);
  }
}

async function loadReservations() {
  const data = await api("/api/reservations");
  const list = $("#reservations-list");
  if (!data.reservations.length) {
    list.innerHTML = `<p class="empty">등록된 예약이 없습니다.</p>`;
    return;
  }
  list.innerHTML = "";
  data.reservations.forEach((r) => {
    const item = document.createElement("div");
    item.className = "list-item";
    const when = new Date(r.scheduled_at * 1000).toLocaleString("ko-KR");
    const plan = r.plan || {};
    const statusCls = "res-" + r.status;
    item.innerHTML = `
      <div class="li-head">
        <div>
          <div class="li-title">${esc(plan.purpose_label || r.purpose)} · ${esc(r.phone)}</div>
          <div class="li-meta">${esc(plan.goal || "")}</div>
          <div class="li-meta">전화 걸 시각: ${esc(when)}</div>
        </div>
        <span class="res-status ${statusCls}">${statusLabel(r.status)}</span>
      </div>
    `;
    if (r.status === "scheduled") {
      const btn = document.createElement("button");
      btn.className = "btn btn-danger btn-block";
      btn.style.marginTop = "14px";
      btn.innerHTML = '<span aria-hidden="true">✕</span> 이 예약 취소하기';
      btn.addEventListener("click", async () => {
        if (!confirm(`${plan.purpose_label || r.purpose} 예약을 정말 취소할까요?\n\n${plan.goal || ""}\n(${when})`)) return;
        try {
          await api("/api/reservations/cancel", { method: "POST", body: { id: r.id } });
          toast("예약을 취소했어요.");
          loadReservations();
        } catch (err) { toast(err.message); }
      });
      item.appendChild(btn);
    }
    list.appendChild(item);
  });
}

// 저장된 대화(transcript)를 자막 말풍선 HTML 로 변환 (기록 화면용)
function renderTranscriptHtml(transcript) {
  return transcript.map((ev) => {
    const role = ev.role === "ai" ? "ai" : ev.role === "other" ? "other" : "system";
    const whoText = role === "ai" ? "AI 도우미" : role === "other" ? "상대방" : "";
    const kind = ev.kind === "decision" ? " (확인 요청)" : "";
    const who = role !== "system"
      ? `<div class="who">${esc(whoText)}${kind}</div>` : "";
    return `<div class="bubble ${role}">${who}<div>${esc(ev.text || "")}</div></div>`;
  }).join("");
}

// ---------- 기록 ----------
async function loadHistory() {
  const data = await api("/api/calls");
  const list = $("#history-list");
  const ended = data.calls.filter((c) => c.status === "ended");
  if (!ended.length) {
    list.innerHTML = `<p class="empty">통화 기록이 없습니다.</p>`;
    return;
  }
  list.innerHTML = "";
  ended.forEach((c) => {
    const item = document.createElement("div");
    item.className = "list-item";
    const when = new Date(c.created_at * 1000).toLocaleString("ko-KR");
    const plan = c.plan || {};
    const cls = "outcome-" + (c.outcome || "needs_action");
    const convo = Array.isArray(c.transcript) ? c.transcript : [];
    const convoHtml = convo.length ? renderTranscriptHtml(convo)
      : '<p class="hint">저장된 대화 내용이 없습니다.</p>';

    item.innerHTML = `
      <div class="li-head">
        <div>
          <div class="li-title">${esc(plan.purpose_label || c.purpose)} · ${esc(c.phone)}</div>
          <div class="li-meta">${esc(when)} · ${modeLabel(c.mode)}</div>
        </div>
        <span class="outcome-pill ${cls}">${outcomeLabel(c.outcome)}</span>
      </div>
      <div class="result-card" style="margin-top:12px;background:var(--bg)">${esc(c.summary || "")}</div>
      <details class="convo-detail">
        <summary>전체 대화 내용 보기 (${convo.filter((e) => e.role !== "system").length}개 대화)</summary>
        <div class="transcript transcript-saved">${convoHtml}</div>
      </details>
    `;
    const del = document.createElement("button");
    del.className = "btn btn-ghost";
    del.textContent = "기록 삭제";
    del.addEventListener("click", async () => {
      await api("/api/calls/delete", { method: "POST", body: { id: c.id } });
      toast("기록을 삭제했습니다.");
      loadHistory();
    });
    item.appendChild(del);
    list.appendChild(item);
  });
}

// ---------- 접근성 ----------
function initSettings() {
  $("#font-scale").addEventListener("input", (e) => {
    const v = parseFloat(e.target.value);
    document.documentElement.style.setProperty("--font-scale", v);
    $("#font-scale-val").textContent = Math.round(v * 100) + "%";
  });
  $("#high-contrast").addEventListener("change", (e) => {
    document.body.classList.toggle("high-contrast", e.target.checked);
  });
  $("#highlight-new").addEventListener("change", (e) => {
    State.highlightNew = e.target.checked;
  });
  $("#save-settings").addEventListener("click", async () => {
    const settings = {
      fontScale: parseFloat($("#font-scale").value),
      highContrast: $("#high-contrast").checked,
      highlightNew: $("#highlight-new").checked,
    };
    try {
      await api("/api/accessibility", { method: "POST", body: { accessibility: settings } });
      $("#settings-saved").textContent = "설정이 저장되었습니다.";
      setTimeout(() => ($("#settings-saved").textContent = ""), 2000);
    } catch (err) { toast(err.message); }
  });
}

function applyAccessibility(s) {
  if (s.fontScale) {
    document.documentElement.style.setProperty("--font-scale", s.fontScale);
    $("#font-scale").value = s.fontScale;
    $("#font-scale-val").textContent = Math.round(s.fontScale * 100) + "%";
  }
  if (s.highContrast) { document.body.classList.add("high-contrast"); $("#high-contrast").checked = true; }
  State.highlightNew = s.highlightNew !== false;
  $("#highlight-new").checked = State.highlightNew;
}

// ---------- 유틸 ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function labelFor(k) {
  return { place: "장소", department: "진료과", date: "날짜", time: "시간",
    party_size: "인원", inquiry: "문의" }[k] || k;
}
function modeLabel(m) {
  return { assisted: "AI 보조 통화", auto: "AI 자율 통화", manual: "텍스트 직접 통화" }[m] || m;
}
function engineLabel(e) {
  return { voice: "실제 AI 음성", phone: "실제 전화", sim: "시뮬레이션 (백업)" }[e] || "실제 AI 음성";
}
function outcomeLabel(o) {
  return { success: "목적 달성", failed: "미달성", needs_action: "추가 조치 필요" }[o] || "확인 필요";
}
function statusLabel(s) {
  return { scheduled: "예약됨", dispatched: "발신 중", done: "완료", canceled: "취소됨", failed: "실패" }[s] || s;
}
function toLocalInput(dt) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
}

// ---------- 부트스트랩 ----------
async function boot() {
  initAuth();
  initNav();
  initNewCall();
  initCallControls();
  initDevicePicker();
  initSettings();

  if (State.token) {
    try {
      const data = await api("/api/me");
      State.user = data.user;
      enterApp();
      return;
    } catch (_) {
      State.token = null;
      localStorage.removeItem("cp_token");
    }
  }
  $("#auth-view").hidden = false;
}

boot();
