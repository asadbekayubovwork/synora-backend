#!/usr/bin/env python3
"""Mint a credential for one of the AI microservices.

The secret is printed **once**. There is nowhere it is stored, so there is no
endpoint and no query that can hand it back — losing it means minting a new key
and revoking the old one, which is the correct recovery path anyway.

    python3 devtools/mint_service_key.py --service voice_agent --label "voice, prod"
    python3 devtools/mint_service_key.py --service tts --scopes usage:write,health:read
    python3 devtools/mint_service_key.py --list
    python3 devtools/mint_service_key.py --revoke svc_tts_7f3a1c9e

Hand the other team the single `SYNORA_INTERNAL_KEY` line, and point them at
docs/INTERNAL_API.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


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


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.billing_enums import BillingService
    from app.models.service_api_key import ServiceApiKey
    from app.services.billing import service_key_service

    engine = create_async_engine(env_value("DATABASE_URL") or "")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as db:
            if args.listing:
                keys = list(
                    (
                        await db.execute(
                            select(ServiceApiKey).order_by(ServiceApiKey.created_at)
                        )
                    ).scalars()
                )
                if not keys:
                    print("No service keys. No microservice can report usage.")
                    return 0
                for key in keys:
                    state = "usable" if key.is_usable else "UNUSABLE"
                    used = key.last_used_at.isoformat() if key.last_used_at else "never"
                    print(
                        f"  {key.key_id:32} {state:9} "
                        f"{(key.service.value if key.service else 'any'):12} "
                        f"uses={key.use_count:<6} last={used}  {key.label}"
                    )
                return 0

            if args.revoke:
                await service_key_service.revoke(db, key_id=args.revoke)
                await db.commit()
                print(f"Revoked {args.revoke}. It stops working on the next request.")
                return 0

            service = (
                BillingService(args.service)
                if args.service and args.service != "any"
                else None
            )
            scopes = tuple(s.strip() for s in args.scopes.split(",") if s.strip())
            minted = await service_key_service.mint(
                db,
                label=args.label,
                service=service,
                scopes=scopes,
                lifetime_days=args.days,
            )
            await db.commit()

            print(f"key id:  {minted.key_id}")
            print(f"service: {service.value if service else 'any'}")
            print(f"scopes:  {', '.join(minted.scopes)}")
            print(f"expires: {minted.expires_at.isoformat() if minted.expires_at else 'never'}")
            print()
            print("Give the service exactly this line, over a channel you trust:")
            print()
            print(f"    SYNORA_INTERNAL_KEY={minted.presented}")
            print()
            print("Printed once. Nothing stores the secret, so nothing can show it again.")
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    from argparse import RawDescriptionHelpFormatter

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument(
        "--service",
        choices=["tts", "stt", "chat", "voice_agent", "any"],
        help="bind the key to one service, so a leak cannot act as the others",
    )
    parser.add_argument("--label", default="", help="who this is for, for the audit trail")
    parser.add_argument(
        "--scopes",
        default="sessions:authorize,sessions:report,usage:write,health:read",
        help="comma separated; default is everything",
    )
    parser.add_argument(
        "--days", type=int, default=90, help="lifetime in days; 0 for no expiry (discouraged)"
    )
    parser.add_argument("--list", action="store_true", dest="listing", help="show existing keys")
    parser.add_argument("--revoke", metavar="KEY_ID", help="revoke a key by id")
    args = parser.parse_args()

    if not args.listing and not args.revoke:
        if not args.label:
            parser.error("--label is required when minting: it is the audit trail")
        if not args.service:
            parser.error(
                "--service is required when minting. An unbound key can act as every "
                "service, which turns one leak into all of them. Pass --service any "
                "only if you really mean it."
            )
    if args.days == 0:
        args.days = None

    if not env_value("DATABASE_URL"):
        print("DATABASE_URL is not set, and .env does not define it.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
