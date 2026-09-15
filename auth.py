"""
auth.py — хеширование паролей и токены сессий для api_server.py.

Токен хранится в БД только хешем (sha256) — сам исходный токен возвращается
клиенту один раз при входе и больше нигде не хранится в открытом виде,
аналогично тому, как хранится пароль (bcrypt). Утечка БД не должна означать
возможность действовать от чужого имени.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

import bcrypt

# Обычный вход живёт короче — на случай, если инженер забыл поставить
# «запомнить меня» на чужом компьютере. «Запомнить меня» рассчитан на то,
# что токен осядет в Диспетчере учётных данных Windows на личном ПК
# инженера и переживёт много перезапусков приложения.
TOKEN_TTL_HOURS = 12
REMEMBER_TTL_DAYS = 30


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(remember_me: bool) -> datetime:
    if remember_me:
        return datetime.now() + timedelta(days=REMEMBER_TTL_DAYS)
    return datetime.now() + timedelta(hours=TOKEN_TTL_HOURS)
