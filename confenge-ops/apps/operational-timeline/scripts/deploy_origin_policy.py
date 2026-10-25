from urllib.parse import urlsplit


CANONICAL_PUBLIC_ORIGIN = "https://gestao.confenge.com.br"
CANONICAL_LOOPBACK_ORIGIN = "http://127.0.0.1:18300"


def _origin_without_extras(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError("URL contains forbidden components")
    return value.rstrip("/")


def validate_public_origin(value: str) -> str:
    normalized = _origin_without_extras(value)
    if normalized != CANONICAL_PUBLIC_ORIGIN:
        raise ValueError("Unexpected public Twenty origin")
    return normalized


def validate_api_origin(value: str) -> str:
    normalized = _origin_without_extras(value)
    if normalized not in (CANONICAL_PUBLIC_ORIGIN, CANONICAL_LOOPBACK_ORIGIN):
        raise ValueError("Unexpected Twenty API origin")
    return normalized
