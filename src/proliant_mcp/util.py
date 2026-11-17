"""Helpers shared by the COM and OneView modules."""

from __future__ import annotations

from typing import Any

# Key-name fragments whose values must never reach the AI model, even through the raw GET tools.
_SECRET_KEY_PARTS = (
    "password", "secret", "token", "sessionid", "sessionkey", "privatekey", "keydata",
    "activationkey", "subscriptionkey", "ssourl", "consoleurl",
)
# Secrets that can hide in an otherwise harmless string value.
_SECRET_VALUE_PARTS = ("sessionkey=", "-----begin")


def _secret_key(key: str) -> bool:
    k = key.lower().replace("-", "").replace("_", "")
    return any(part in k for part in _SECRET_KEY_PARTS)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if v and _secret_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and any(part in value.lower() for part in _SECRET_VALUE_PARTS):
        return "***"
    return value


def matches(query: str, *candidates: Any) -> bool:
    q = query.strip().lower()
    return any(q == str(c).strip().lower() for c in candidates if c)
