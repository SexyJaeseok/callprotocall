"""인증: 회원가입, 로그인, 세션 토큰, 비밀번호 해싱.

비밀번호는 PBKDF2-HMAC-SHA256 으로 솔트와 함께 저장한다.
(FR-001, FR-002, FR-003 — 인증된 사용자만 발신 가능)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from . import db

_ITERATIONS = 120_000


def hash_password(password: str, salt: str | None = None):
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, stored_hash)


def register(email: str, password: str, display_name: str):
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        return None, "유효한 이메일을 입력해 주세요."
    if len(password or "") < 6:
        return None, "비밀번호는 6자 이상이어야 합니다."
    if db.get_user_by_email(email):
        return None, "이미 가입된 이메일입니다."
    pwd_hash, salt = hash_password(password)
    user_id = db.create_user(email, pwd_hash, salt, display_name or email.split("@")[0])
    return user_id, None


def login(email: str, password: str):
    # 아이디는 이메일 형식이 아니어도 된다 (예: dongeui).
    username = (email or "").strip().lower()
    user = db.get_user_by_email(username)
    if not user or not verify_password(password, user["password_hash"], user["salt"]):
        return None, "아이디 또는 비밀번호가 올바르지 않습니다."
    token = secrets.token_urlsafe(32)
    db.create_session(token, user["id"])
    return token, None


# 고정 데모 계정
SEED_USERNAME = "dongeui"
SEED_PASSWORD = "cse915"
SEED_DISPLAY_NAME = "동의대"


def ensure_seed_account():
    """서버 시작 시 고정 로그인 계정을 보장한다 (없으면 생성)."""
    existing = db.get_user_by_email(SEED_USERNAME)
    pwd_hash, salt = hash_password(SEED_PASSWORD)
    if existing:
        # 비밀번호가 바뀌었을 수 있으니 항상 최신 값으로 맞춘다.
        db.set_credentials(existing["id"], pwd_hash, salt)
        return existing["id"]
    return db.create_user(SEED_USERNAME, pwd_hash, salt, SEED_DISPLAY_NAME)


def user_from_token(token: str | None):
    if not token:
        return None
    user_id = db.get_session_user(token)
    if user_id is None:
        return None
    return db.get_user(user_id)


def logout(token: str | None):
    if token:
        db.delete_session(token)
