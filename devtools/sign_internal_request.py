#!/usr/bin/env python3
"""Send a signed request to the internal API, from the command line.

The microservices are not built yet, and even once they are, being able to poke
the internal API by hand is the difference between "the signature is wrong
somewhere" and knowing where. This is also a **second, independent
implementation of the signing scheme** — the same reason
`devtools/telegram_login_payload.py` exists — so a bug in the app's canonical
string shows up as a mismatch here rather than as silence.

    python3 devtools/sign_internal_request.py GET /internal/v1/health
    python3 devtools/sign_internal_request.py POST /internal/v1/debug/echo-signature \\
        --body '{"hello":"world"}'
    python3 devtools/sign_internal_request.py GET /internal/v1/health --print-canonical

The key comes from `SYNORA_INTERNAL_KEY` (`<key_id>.<secret>`, as
`mint_service_key.py` prints it), or from `--key`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def env_value(name: str) -> str | None:
    if value := os.environ.get(name):
        return value
    if not ENV_FILE.exists():
        return None
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if line.startswith(f"{name}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


def canonical_query(query: str) -> str:
    """Sorted and re-encoded. A signature that depends on parameter order
    fails intermittently, which is the worst kind of bug to hand anyone."""
    if not query:
        return ""
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)


def canonical_string(
    method: str, path: str, query: str, timestamp: int, nonce: str, key_id: str, body: bytes
) -> str:
    return "\n".join(
        [
            "SYNORA-HMAC-V1",
            method.upper(),
            path,
            canonical_query(query),
            str(timestamp),
            nonce,
            key_id,
            hashlib.sha256(body).hexdigest(),
        ]
    )


def sign(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return "v1=" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def main() -> int:
    from argparse import RawDescriptionHelpFormatter

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument("method", help="GET, POST, ...")
    parser.add_argument("target", help="path, optionally with a query string")
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="base URL")
    parser.add_argument("--body", default="", help="request body, sent as JSON")
    parser.add_argument("--key", help="<key_id>.<secret>; defaults to SYNORA_INTERNAL_KEY")
    parser.add_argument(
        "--skew",
        type=int,
        default=0,
        help="seconds to add to the timestamp, for testing the skew guard",
    )
    parser.add_argument(
        "--print-canonical", action="store_true", help="print the signed string and exit"
    )
    parser.add_argument("--no-send", action="store_true", help="print the headers and exit")
    args = parser.parse_args()

    presented = args.key or env_value("SYNORA_INTERNAL_KEY")
    if not presented or "." not in presented:
        print(
            "Need a key as <key_id>.<secret>. Set SYNORA_INTERNAL_KEY or pass --key.\n"
            "Mint one with: python3 devtools/mint_service_key.py --service voice_agent "
            '--label "local"',
            file=sys.stderr,
        )
        return 2
    key_id, _, secret = presented.partition(".")

    split = urlsplit(args.target)
    path, query = split.path, split.query
    body = args.body.encode() if args.body else b""
    timestamp = int(time.time()) + args.skew
    nonce = secrets.token_urlsafe(16)

    message = canonical_string(args.method, path, query, timestamp, nonce, key_id, body)
    if args.print_canonical:
        # `repr` so the newlines are visible — an invisible trailing newline is
        # a very common cause of a signature that will not match.
        print(repr(message))
        return 0

    headers = {
        "X-Synora-Key-Id": key_id,
        "X-Synora-Timestamp": str(timestamp),
        "X-Synora-Nonce": nonce,
        "X-Synora-Signature": sign(secret, message),
    }
    if body:
        headers["Content-Type"] = "application/json"

    if args.no_send:
        for name, value in headers.items():
            print(f"{name}: {value}")
        return 0

    url = args.api.rstrip("/") + args.target
    request = urllib.request.Request(url, data=body or None, headers=headers, method=args.method.upper())
    try:
        with urllib.request.urlopen(request) as response:
            print(f"{response.status} {response.reason}")
            print(json.dumps(json.loads(response.read()), indent=2, ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as error:
        payload = error.read()
        print(f"{error.code} {error.reason}", file=sys.stderr)
        try:
            print(json.dumps(json.loads(payload), indent=2, ensure_ascii=False), file=sys.stderr)
        except ValueError:
            print(payload.decode(errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"Could not reach {url}: {error.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
