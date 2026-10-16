"""OpenAI 연동 래퍼 (표준 라이브러리만 사용).

키 관리:
- 환경변수 OPENAI_API_KEY 또는 프로젝트 루트의 .env 파일에서 읽는다.
- 키가 없으면 is_enabled() == False → 호출부는 규칙 기반으로 폴백한다.

API 키는 서버에서만 다루며 클라이언트로 노출하지 않는다 (NFR-005).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_API_URL = "https://api.openai.com/v1/chat/completions"
_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
_TIMEOUT = 30

_api_key: str | None = None
_loaded = False


def _load_env_file():
    """프로젝트 루트의 .env 를 읽어 환경변수에 반영 (python-dotenv 없이)."""
    root = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


def _get_key() -> str | None:
    global _api_key, _loaded
    if not _loaded:
        _load_env_file()
        _api_key = os.environ.get("OPENAI_API_KEY") or None
        _loaded = True
    return _api_key


def is_enabled() -> bool:
    """OpenAI 를 쓸 수 있는 상태인지 (키 존재 여부)."""
    return bool(_get_key())


def chat(messages: list[dict], temperature: float = 0.4,
         max_tokens: int = 500, want_json: bool = False) -> str | None:
    """OpenAI Chat Completions 호출. 실패하면 None 을 반환해 폴백을 유도한다.

    messages: [{"role": "system|user|assistant", "content": "..."}]
    want_json: True 면 response_format=json_object 로 강제.
    """
    key = _get_key()
    if not key:
        return None

    payload = {
        "model": _MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if want_json:
        payload["response_format"] = {"type": "json_object"}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8")
        except Exception:
            err = str(e)
        print(f"[llm] OpenAI HTTPError {e.code}: {err[:300]}", flush=True)
        return None
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"[llm] OpenAI 호출 실패: {e}", flush=True)
        return None


def chat_json(messages: list[dict], temperature: float = 0.4,
              max_tokens: int = 500) -> dict | None:
    """JSON 응답을 파싱해 dict 로 반환. 실패 시 None."""
    raw = chat(messages, temperature=temperature, max_tokens=max_tokens, want_json=True)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        # 모델이 JSON 외 텍스트를 섞은 경우 중괄호 구간만 추출 시도
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except (ValueError, TypeError):
                return None
        return None
