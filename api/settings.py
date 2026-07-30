import os
from urllib.parse import urlparse

from dotenv import load_dotenv


load_dotenv()


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _join_url(base_url: str, path: str = "") -> str:
    base_url = _strip_trailing_slash(base_url)
    if not path:
        return base_url
    return f"{base_url}/{path.lstrip('/')}"


def get_api_base_url() -> str:
    return _strip_trailing_slash(os.getenv("API_BASE_URL", "http://localhost:8000"))


def get_frontend_base_url() -> str:
    return _strip_trailing_slash(
        os.getenv("FRONTEND_BASE_URL", "http://localhost:3000")
    )


def get_api_url(path: str = "") -> str:
    return _join_url(get_api_base_url(), path)


def get_frontend_url(path: str = "") -> str:
    return _join_url(get_frontend_base_url(), path)


def get_cors_allowed_origins() -> list[str]:
    origins = os.getenv("CORS_ALLOWED_ORIGINS")
    if not origins:
        return [get_frontend_base_url()]

    return [origin.strip().rstrip("/") for origin in origins.split(",") if origin.strip()]


def _env_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None

    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_cookie_secure() -> bool:
    secure_override = _env_bool("SESSION_COOKIE_SECURE")
    if secure_override is not None:
        return secure_override

    return urlparse(get_api_base_url()).scheme == "https"


def get_cookie_samesite() -> str:
    samesite = os.getenv("SESSION_COOKIE_SAMESITE")
    if samesite:
        return samesite.strip().lower()

    return "none" if get_cookie_secure() else "lax"


def get_cookie_settings() -> dict[str, bool | str]:
    return {
        "secure": get_cookie_secure(),
        "samesite": get_cookie_samesite(),
    }