"""Meta webhook signature check.

Meta signs every webhook POST: X-Hub-Signature-256 is "sha256=" followed by
the hex HMAC-SHA256 of the raw request body, keyed with the app secret
(App settings > Basic in the App Dashboard). Verify against the raw bytes as
received; re-serialising parsed JSON changes whitespace and escaping (Meta
escapes non-ASCII characters as \\uXXXX) and breaks the match.
"""
import hashlib
import hmac

PREFIX = "sha256="


def compute_signature(secret: str, raw_body: bytes) -> str:
    return PREFIX + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def is_valid_signature(secret: str, raw_body: bytes, header_value: str | None) -> bool:
    if not secret or not header_value:
        return False
    header_value = header_value.strip()
    if not header_value.lower().startswith(PREFIX):
        return False
    expected = compute_signature(secret, raw_body)
    return hmac.compare_digest(expected, PREFIX + header_value[len(PREFIX):].lower())
