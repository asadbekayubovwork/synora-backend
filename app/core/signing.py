"""Request signing for the microservices that call us.

The problem this solves: four AI services need to report usage into this API,
and a bearer token in a header is not enough on its own. A leaked token is
replayable, and a token that authenticates the *caller* says nothing about
whether the body arrived intact. So every internal request carries an
HMAC-SHA256 signature over a canonical description of itself.

## The secret is derived, not stored

There is a real conflict hiding in "store a hash of the API key": verifying an
HMAC needs the plaintext secret on the server, so hashing it at rest and
verifying a signature with it cannot both be true.

The resolution is to *derive* the secret from one master key:

    secret(key_id) = HMAC-SHA256(INTERNAL_KEY_SECRET, "synora-internal-key|v1|" + key_id)

which keeps the property that actually mattered — a database dump yields no
usable credential, because the `service_api_keys` row holds nothing secret at
all. Revocation stays a single `UPDATE`: instant, no deploy, no key
distribution.

The cost, stated plainly: compromising `INTERNAL_KEY_SECRET` compromises every
service key at once. That is the same blast radius as `JWT_SECRET`, which this
codebase already accepts and guards in `assert_production_ready()`. The
strictly better design is Ed25519 with only a public key stored — no shared
secret anywhere — and it is the upgrade path, worth taking the day
`cryptography` becomes a dependency for some other reason. It is not today,
because adding a native wheel to a `tar czf app requirements.txt` deploy is a
real cost for a threat this design already bounds.

## Why sign a canonical string rather than just the body

Signing the body alone lets a captured request be replayed against a *different
endpoint*. Signing the body and the path lets it be replayed forever. So the
canonical string pins the method, the path, the query, a timestamp, a nonce and
the key id, and the timestamp plus nonce bound replay to a five-minute window
that a Redis set closes entirely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote

from app.core.config import settings

SIGNATURE_VERSION = "v1"
CANONICAL_PREFIX = "SYNORA-HMAC-V1"

HEADER_KEY_ID = "X-Synora-Key-Id"
HEADER_TIMESTAMP = "X-Synora-Timestamp"
HEADER_NONCE = "X-Synora-Nonce"
HEADER_SIGNATURE = "X-Synora-Signature"
HEADER_TRACE_ID = "X-Synora-Trace-Id"

# sha256 of an empty body, precomputed so a GET does not have to special-case.
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()

_KEY_ID_ALPHABET = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
_NONCE_ALPHABET = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def derive_secret(key_id: str, *, version: int = 1) -> str:
    """The secret half of a key, from its public half.

    Deterministic, so a key can be re-derived rather than recovered — there is
    nowhere it could be recovered *from*. `version` exists so the master secret
    can be rotated with an overlap window instead of a flag day.
    """
    if not key_id:
        raise ValueError("key_id must not be empty")
    master = settings.internal_key_secret.encode("utf-8")
    label = f"synora-internal-key|v{version}|{key_id}".encode()
    return _b64url(hmac.new(master, label, hashlib.sha256).digest())


def new_key_id(service: str | None) -> str:
    """A fresh public key id. `svc_voice_agent_7f3a1c9e`, or `svc_any_…`."""
    return f"svc_{service or 'any'}_{secrets.token_hex(4)}"


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def is_valid_key_id(value: str) -> bool:
    return bool(value) and 4 <= len(value) <= 64 and set(value) <= _KEY_ID_ALPHABET


def is_valid_nonce(value: str) -> bool:
    return bool(value) and 16 <= len(value) <= 128 and set(value) <= _NONCE_ALPHABET


def canonical_query(query: str) -> str:
    """Query parameters, sorted and re-encoded, or an empty string.

    Sorted because a proxy or an HTTP client may reorder them, and a signature
    that depends on the order of a dict is a signature that fails intermittently
    — which is the worst possible failure mode to hand another team.
    """
    if not query:
        return ""
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return "&".join(
        f"{quote(name, safe='')}={quote(value, safe='')}" for name, value in pairs
    )


def body_digest(body: bytes) -> str:
    """sha256 of the raw request bytes, lowercase hex.

    Raw bytes, never re-serialised JSON. Canonicalising JSON before hashing is
    the classic source of cross-language interop bugs: key order, unicode
    escaping and float formatting all differ between our parser and theirs, so
    two sides that agree on the *meaning* of a payload disagree on its bytes.
    """
    return hashlib.sha256(body).hexdigest()


def canonical_string(
    *,
    method: str,
    path: str,
    query: str,
    timestamp: str,
    nonce: str,
    key_id: str,
    body: bytes,
) -> str:
    """The eight lines that get signed. Newline-joined, no trailing newline."""
    return "\n".join(
        [
            CANONICAL_PREFIX,
            method.upper(),
            path,
            canonical_query(query),
            timestamp,
            nonce,
            key_id,
            body_digest(body),
        ]
    )


def sign(secret: str, canonical: str) -> str:
    """The `X-Synora-Signature` value: `v1=<base64url>`."""
    digest = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256)
    return f"{SIGNATURE_VERSION}={_b64url(digest.digest())}"


def verify(secret: str, canonical: str, presented: str) -> bool:
    """Constant-time comparison, and a version check.

    `compare_digest` rather than `==` because a byte-by-byte comparison leaks
    how much of a forged signature was correct, which is enough to construct
    one given enough attempts.
    """
    if not presented or "=" not in presented:
        return False
    version, _, value = presented.partition("=")
    if version != SIGNATURE_VERSION:
        return False
    expected = sign(secret, canonical)
    return hmac.compare_digest(expected, f"{SIGNATURE_VERSION}={value}")


@dataclass(frozen=True)
class SignedRequest:
    """Everything a client needs to send. Returned by the signing helpers so
    the reference implementation and the tests build requests the same way."""

    headers: dict[str, str]
    canonical: str


def sign_request(
    *,
    key_id: str,
    secret: str,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    timestamp: int,
    nonce: str | None = None,
    trace_id: str | None = None,
) -> SignedRequest:
    """Build the four signature headers for one request.

    `timestamp` is passed in rather than read from the clock so that a caller
    can be tested deterministically, and so the skew handling is exercised
    rather than assumed.
    """
    chosen_nonce = nonce or new_nonce()
    stamp = str(timestamp)
    canonical = canonical_string(
        method=method,
        path=path,
        query=query,
        timestamp=stamp,
        nonce=chosen_nonce,
        key_id=key_id,
        body=body,
    )
    headers = {
        HEADER_KEY_ID: key_id,
        HEADER_TIMESTAMP: stamp,
        HEADER_NONCE: chosen_nonce,
        HEADER_SIGNATURE: sign(secret, canonical),
    }
    if trace_id:
        headers[HEADER_TRACE_ID] = trace_id
    return SignedRequest(headers=headers, canonical=canonical)
