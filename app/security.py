from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings

password_hasher = PasswordHasher()
SENSITIVE_METADATA_TERMS = (
    "access_token",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "refresh_token",
    "secret",
)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hmac.new(
        settings.app_secret_key.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=settings.session_ttl_seconds)


def sanitize_metadata(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if any(term in key.lower() for term in SENSITIVE_METADATA_TERMS)
            else sanitize_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_metadata(item) for item in value]
    return value


def constant_time_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
