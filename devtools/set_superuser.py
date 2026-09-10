#!/usr/bin/env python3
"""Grant or revoke admin rights on an account.

The admin routes move real money — a manual credit, a refund, publishing a
price — so `users.is_superuser` is not settable through the API. There is no
"promote yourself" endpoint and there should not be one until there is a UI
with a review step behind it. Until then, this is the tool, and it needs shell
access to the box.

Reads DATABASE_URL from the environment or from `.env`, the same way
`devtools/telegram_login_payload.py` reads its bot token — deliberately not
through `Settings`, so a broken config cannot stop you fixing a broken config.

    python3 devtools/set_superuser.py ali@example.com
    python3 devtools/set_superuser.py ali@example.com --revoke
    python3 devtools/set_superuser.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def env_value(name: str) -> str | None:
    """The environment first, then a line-parse of `.env`."""
    if value := os.environ.get(name):
        return value
    if not ENV_FILE.exists():
        return None
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if line.startswith(f"{name}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


async def run(database_url: str, email: str | None, revoke: bool, listing: bool) -> int:
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import create_async_engine

    # Imported here, after the URL is known, so a missing .env fails with a
    # readable message rather than an import-time crash.
    from app.models.user import User, normalize_email

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            if listing:
                rows = (
                    await conn.execute(
                        select(User.email, User.id).where(User.is_superuser.is_(True))
                    )
                ).all()
                if not rows:
                    print("No admins. Nobody can reach /api/v1/admin/*.")
                    return 0
                print(f"{len(rows)} admin(s):")
                for row_email, row_id in rows:
                    print(f"  {row_email or '(no email)'}  {row_id}")
                return 0

            assert email is not None
            normalized = normalize_email(email)
            result = await conn.execute(
                update(User)
                .where(User.email == normalized)
                .values(is_superuser=not revoke)
                .returning(User.id, User.is_verified)
            )
            row = result.first()
            if row is None:
                print(f"No account with email {normalized!r}.", file=sys.stderr)
                return 1

            user_id, is_verified = row
            verb = "revoked from" if revoke else "granted to"
            print(f"Admin {verb} {normalized} ({user_id})")
            if not revoke and not is_verified:
                # `get_current_user` refuses an unverified account before it
                # ever reaches the admin check, so this would look like a
                # working admin that 403s on every call.
                print(
                    "Warning: this account is not verified, so it cannot sign in at all yet.",
                    file=sys.stderr,
                )
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email", nargs="?", help="the account to change")
    parser.add_argument("--revoke", action="store_true", help="take admin away instead")
    parser.add_argument("--list", action="store_true", dest="listing", help="show current admins")
    args = parser.parse_args()

    if not args.listing and not args.email:
        parser.error("give an email, or --list")

    database_url = env_value("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set, and .env does not define it.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    return asyncio.run(run(database_url, args.email, args.revoke, args.listing))


if __name__ == "__main__":
    raise SystemExit(main())
