from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./sarguardian.db")
    app_secret_key: str = os.getenv(
        "APP_SECRET_KEY", "local-development-only-change-this-secret"
    )
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "sarguardian_session")
    csrf_cookie_name: str = os.getenv("CSRF_COOKIE_NAME", "sarguardian_csrf")
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    cookie_samesite: str = os.getenv("COOKIE_SAMESITE", "lax")
    science_mode: str = os.getenv("SCIENCE_MODE", "mock").lower()
    science_result_root: str = os.getenv("SCIENCE_RESULT_ROOT", "./science-results")
    science_root: str = os.getenv("SCIENCE_ROOT", "./science-source")
    science_real_command: str = os.getenv("SCIENCE_REAL_COMMAND", "")
    science_expected_commit: str = os.getenv(
        "SCIENCE_EXPECTED_COMMIT", "77cc9646cfa46d3aff3669d351912f984cf67aa3"
    )
    drive_mode: str = os.getenv("DRIVE_MODE", "mock").lower()


settings = Settings()
