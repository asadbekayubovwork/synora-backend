#!/usr/bin/env python3
"""Sign a Telegram login payload the way the widget would.

BotFather's /setdomain will not accept `localhost`, so the widget itself cannot
load on a dev machine. The signature is the whole of the authentication though,
and it is computable from the bot token — so this produces a payload the API
accepts, which is enough to exercise everything behind the endpoint.

    python3 devtools/telegram_login_payload.py            # print it
    python3 devtools/telegram_login_payload.py --post     # and send it

Only proves the endpoint. Whether the real widget renders and hands back the
same shape still needs a real domain.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def bot_token_from_env() -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.partition("=")[2].strip().strip("'\"") or None
    return None


def sign(payload: dict[str, object], bot_token: str) -> dict[str, object]:
    """`key=value` for every field, newline joined, key sorted, HMAC'd."""
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    digest = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", help="Bot token. Defaults to TELEGRAM_BOT_TOKEN in .env")
    parser.add_argument("--id", type=int, default=987654321, help="Telegram user id")
    parser.add_argument("--first-name", default="Ali")
    parser.add_argument("--last-name", default="Valiyev")
    parser.add_argument("--username", default="ali")
    parser.add_argument("--post", action="store_true", help="POST it to the callback endpoint")
    parser.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument(
        "--path",
        default="/auth/oauth/telegram/callback",
        help="Use /auth/oauth/telegram/link to link instead (needs --bearer)",
    )
    parser.add_argument("--bearer", help="Access token, for the link endpoint")
    args = parser.parse_args()

    bot_token = args.token or bot_token_from_env()
    if not bot_token:
        print("No bot token. Pass --token, or set TELEGRAM_BOT_TOKEN in .env.")
        return 1

    payload = sign(
        {
            "id": args.id,
            "first_name": args.first_name,
            "last_name": args.last_name,
            "username": args.username,
            "photo_url": f"https://t.me/i/userpic/320/{args.username}.jpg",
            "auth_date": int(time.time()),
        },
        bot_token,
    )
    body = json.dumps(payload, indent=2)
    print(body)

    if not args.post:
        print("\nPost it yourself, or re-run with --post.")
        return 0

    request = urllib.request.Request(
        args.api.rstrip("/") + args.path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {args.bearer}"} if args.bearer else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f"\n{response.status}\n{json.dumps(json.loads(response.read()), indent=2)}")
    except urllib.error.HTTPError as exc:
        print(f"\n{exc.code}\n{exc.read().decode(errors='replace')}")
        return 1
    except OSError as exc:
        print(f"\nCould not reach {args.api}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
