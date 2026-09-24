from datetime import datetime, timezone
import re

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import SessionRecord, User
from app.schemas import Credentials, MessageResponse, UserResponse
from app.security import (
    hash_password,
    hash_session_token,
    new_opaque_token,
    normalize_email,
    session_expiry,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="Invalid email address")
    return normalized


def _set_auth_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def _create_session(db: Session, user: User) -> tuple[str, str]:
    session_token = new_opaque_token()
    csrf_token = new_opaque_token()
    db.add(
        SessionRecord(
            user_id=user.id,
            token_hash=hash_session_token(session_token),
            expires_at=session_expiry(),
        )
    )
    db.commit()
    return session_token, csrf_token


def _user_response(user: User) -> UserResponse:
    return UserResponse(id=user.id, email=user.email, created_at=user.created_at)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> User:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    session_record = db.scalar(
        select(SessionRecord).where(SessionRecord.token_hash == hash_session_token(session_token))
    )
    now = datetime.now(timezone.utc)
    expires_at = session_record.expires_at if session_record is not None else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        session_record is None
        or session_record.revoked_at is not None
        or expires_at <= now
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    user = db.get(User, session_record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    request.state.session_record = session_record
    return user


def require_csrf(
    request: Request,
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    csrf_cookie: str | None = Cookie(default=None, alias=settings.csrf_cookie_name),
) -> None:
    if not csrf_header or not csrf_cookie or not hmac_compare(csrf_header, csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(credentials: Credentials, response: Response, db: Session = Depends(get_db)) -> UserResponse:
    email = _validate_email(credentials.email)
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(email=email, password_hash=hash_password(credentials.password))
    db.add(user)
    db.flush()
    session_token, csrf_token = _create_session(db, user)
    _set_auth_cookies(response, session_token, csrf_token)
    return _user_response(user)


@router.post("/login", response_model=UserResponse)
def login(credentials: Credentials, response: Response, db: Session = Depends(get_db)) -> UserResponse:
    email = _validate_email(credentials.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(user.password_hash, credentials.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    session_token, csrf_token = _create_session(db, user)
    _set_auth_cookies(response, session_token, csrf_token)
    return _user_response(user)


@router.post("/logout", response_model=MessageResponse)
def logout(
    response: Response,
    request: Request,
    _user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MessageResponse:
    session_record = request.state.session_record
    session_record.revoked_at = datetime.now(timezone.utc)
    db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> UserResponse:
    return _user_response(user)
