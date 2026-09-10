"""The wallet and admin routes, over HTTP."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update

from app.api.v1 import admin as admin_routes
from app.models.user import User
from app.models.wallet import Wallet
from app.services.billing import wallet_repo, wallet_service
from tests.conftest import auth, register_and_verify

CREDIT = 1_000_000


async def _make_admin(db, email: str) -> None:
    await db.execute(update(User).where(User.email == email).values(is_superuser=True))
    await db.commit()


# --- the balance -----------------------------------------------------------


async def test_a_new_account_reports_an_empty_wallet(client):
    tokens = await register_and_verify(client)

    response = await client.get("/wallet", headers=auth(tokens["access_token"]))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["available_micros"] == 0
    assert body["available"] == "0.000000"
    assert body["is_frozen"] is False
    assert body["micros_per_credit"] == CREDIT


async def test_every_amount_comes_with_a_display_string(client, session):
    """The integer is for arithmetic; the string is so a JS client never has to
    divide by a million and hope."""
    tokens = await register_and_verify(client)
    user_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    from app.models.billing_enums import LedgerEntryKind, LedgerRefType

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, paid_micros=36_633_183,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )
    await session.commit()

    body = (await client.get("/wallet", headers=auth(tokens["access_token"]))).json()

    assert body["paid_micros"] == 36_633_183
    assert body["paid"] == "36.633183"
    assert body["available"] == "36.633183"


async def test_the_wallet_needs_a_bearer_token(client):
    response = await client.get("/wallet")

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


async def test_reserved_credit_is_shown_but_not_spendable(client, session):
    tokens = await register_and_verify(client)
    user_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    from app.models.billing_enums import LedgerEntryKind, LedgerRefType

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, paid_micros=10 * CREDIT,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )
    await session.commit()

    body = (await client.get("/wallet", headers=auth(tokens["access_token"]))).json()

    assert body["paid_micros"] == 10 * CREDIT
    assert body["reserved_micros"] == 4 * CREDIT
    assert body["available_micros"] == 6 * CREDIT


# --- the statement ---------------------------------------------------------


async def test_the_statement_is_empty_for_a_new_account(client):
    tokens = await register_and_verify(client)

    body = (
        await client.get("/wallet/transactions", headers=auth(tokens["access_token"]))
    ).json()

    assert body["entries"] == []
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None


async def test_the_statement_pages_newest_first_without_gaps_or_repeats(client, session):
    tokens = await register_and_verify(client)
    user_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    from app.models.billing_enums import LedgerEntryKind, LedgerRefType

    for n in range(12):
        await wallet_repo.credit(
            session, wallet_id=snapshot.wallet_id, paid_micros=CREDIT,
            kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP,
            idempotency_key=f"t{n}",
        )
    await session.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous bound; 12 rows at 5 a page is 3 requests
        url = "/wallet/transactions?limit=5" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(url, headers=auth(tokens["access_token"]))).json()
        seen.extend(entry["id"] for entry in body["entries"])
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            break

    assert len(seen) == 12
    assert len(set(seen)) == 12, "no row appeared twice"


async def test_pagination_is_stable_while_new_rows_arrive(client, session):
    """The reason this is keyset and not offset.

    With OFFSET, inserting between two requests shifts the window: the reader
    sees a row twice and never sees another one at all.
    """
    tokens = await register_and_verify(client)
    user_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    from app.models.billing_enums import LedgerEntryKind, LedgerRefType

    async def topup(key: str) -> None:
        await wallet_repo.credit(
            session, wallet_id=snapshot.wallet_id, paid_micros=CREDIT,
            kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key=key,
        )
        await session.commit()

    for n in range(6):
        await topup(f"old{n}")

    first = (
        await client.get("/wallet/transactions?limit=3", headers=auth(tokens["access_token"]))
    ).json()
    page_one = [entry["id"] for entry in first["entries"]]

    # Three more land between the two requests.
    for n in range(3):
        await topup(f"new{n}")

    second = (
        await client.get(
            f"/wallet/transactions?limit=3&cursor={first['page']['next_cursor']}",
            headers=auth(tokens["access_token"]),
        )
    ).json()
    page_two = [entry["id"] for entry in second["entries"]]

    assert not set(page_one) & set(page_two), "no row served twice"
    assert len(page_two) == 3, "and none skipped"


async def test_a_forged_cursor_is_refused_cleanly(client):
    tokens = await register_and_verify(client)

    response = await client.get(
        "/wallet/transactions?cursor=not-a-real-cursor", headers=auth(tokens["access_token"])
    )

    assert response.status_code == 400
    assert response.json()["code"] == "cursor_invalid"


async def test_one_user_cannot_read_another_statement(client, session):
    first = await register_and_verify(client, email="ali@example.com")
    await register_and_verify(client, email="bek@example.com")
    bek_id = (
        await session.execute(select(User.id).where(User.email == "bek@example.com"))
    ).scalar_one()
    snapshot = await wallet_service.ensure_wallet(session, bek_id)
    from app.models.billing_enums import LedgerEntryKind, LedgerRefType

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, paid_micros=CREDIT,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )
    await session.commit()

    body = (
        await client.get("/wallet/transactions", headers=auth(first["access_token"]))
    ).json()

    assert body["entries"] == []


async def test_a_split_charge_shows_both_halves_under_one_group(client, session):
    tokens = await register_and_verify(client)
    user_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    from app.models.billing_enums import LedgerEntryKind, LedgerRefType

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, paid_micros=5 * CREDIT,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )
    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, bonus_micros=2 * CREDIT,
        kind=LedgerEntryKind.BONUS_GRANT, ref_type=LedgerRefType.ADMIN_GRANT,
        idempotency_key="b1",
    )
    await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=3 * CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
    )
    await session.commit()

    body = (
        await client.get("/wallet/transactions", headers=auth(tokens["access_token"]))
    ).json()

    debits = [e for e in body["entries"] if e["kind"] == "debit"]
    assert len(debits) == 2
    assert len({e["group_id"] for e in debits}) == 1
    assert sorted(e["bucket"] for e in debits) == ["bonus", "paid"]
    assert sum(e["amount_micros"] for e in debits) == -3 * CREDIT


# --- admin -----------------------------------------------------------------


async def test_admin_routes_refuse_an_ordinary_user(client):
    tokens = await register_and_verify(client)

    response = await client.post(
        f"/admin/wallets/{uuid.uuid4()}/credits",
        headers={**auth(tokens["access_token"]), "Idempotency-Key": "k1"},
        json={"amount_micros": CREDIT, "bucket": "paid", "note": "test"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"


async def test_a_superuser_can_grant_credit(client, session):
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")
    target = await register_and_verify(client, email="ali@example.com")
    target_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()

    response = await client.post(
        f"/admin/wallets/{target_id}/credits",
        headers={**auth(tokens["access_token"]), "Idempotency-Key": "grant-1"},
        json={"amount_micros": 50 * CREDIT, "bucket": "paid", "note": "Goodwill for #482"},
    )

    assert response.status_code == 200
    assert response.json()["paid_micros"] == 50 * CREDIT
    # And the recipient sees it.
    mine = (await client.get("/wallet", headers=auth(target["access_token"]))).json()
    assert mine["available_micros"] == 50 * CREDIT


async def test_a_manual_credit_without_an_idempotency_key_is_refused(client, session):
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")

    response = await client.post(
        f"/admin/wallets/{uuid.uuid4()}/credits",
        headers=auth(tokens["access_token"]),
        json={"amount_micros": CREDIT, "bucket": "paid", "note": "test"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_required"


async def test_a_replayed_manual_credit_grants_once(client, session):
    """A double-submitted manual credit is real money."""
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")
    await register_and_verify(client, email="ali@example.com")
    target_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()

    for _ in range(3):
        response = await client.post(
            f"/admin/wallets/{target_id}/credits",
            headers={**auth(tokens["access_token"]), "Idempotency-Key": "one-and-only"},
            json={"amount_micros": 10 * CREDIT, "bucket": "paid", "note": "Same key thrice"},
        )
        assert response.status_code == 200

    assert response.json()["paid_micros"] == 10 * CREDIT


async def test_an_idempotency_key_at_the_published_length_is_granted(client, session):
    """The length the OpenAPI page advertises has to be a length that works.

    It was not: the header published no ceiling at all — "any unique string" —
    while `admin:` plus the key goes into a VARCHAR(128), so a 200-character
    key was 22001 and a 500 on Postgres and a silent full-width store on
    SQLite. Both ends of the boundary are checked here, because a ceiling that
    refuses the value it publishes is the same bug wearing a 4xx.
    """
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")
    await register_and_verify(client, email="ali@example.com")
    target_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()

    at_limit = "k" * admin_routes.MAX_ADMIN_IDEMPOTENCY_KEY
    response = await client.post(
        f"/admin/wallets/{target_id}/credits",
        headers={**auth(tokens["access_token"]), "Idempotency-Key": at_limit},
        json={"amount_micros": 10 * CREDIT, "bucket": "paid", "note": "At the ceiling"},
    )

    assert response.status_code == 200
    assert response.json()["paid_micros"] == 10 * CREDIT
    # And it replayed rather than granted twice, which is only true if the key
    # was stored whole.
    again = await client.post(
        f"/admin/wallets/{target_id}/credits",
        headers={**auth(tokens["access_token"]), "Idempotency-Key": at_limit},
        json={"amount_micros": 10 * CREDIT, "bucket": "paid", "note": "At the ceiling"},
    )
    assert again.status_code == 200
    assert again.json()["paid_micros"] == 10 * CREDIT


async def test_an_over_long_idempotency_key_is_refused_by_validation(client, session):
    """A 422 naming the header, not a 500 out of the database driver."""
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")

    response = await client.post(
        f"/admin/wallets/{uuid.uuid4()}/credits",
        headers={
            **auth(tokens["access_token"]),
            "Idempotency-Key": "k" * (admin_routes.MAX_ADMIN_IDEMPOTENCY_KEY + 1),
        },
        json={"amount_micros": CREDIT, "bucket": "paid", "note": "One character over"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_a_credit_needs_a_reason(client, session):
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")

    response = await client.post(
        f"/admin/wallets/{uuid.uuid4()}/credits",
        headers={**auth(tokens["access_token"]), "Idempotency-Key": "k1"},
        json={"amount_micros": CREDIT, "bucket": "paid", "note": "x"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_freezing_a_wallet_stops_it_being_used(client, session):
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")
    await register_and_verify(client, email="ali@example.com")
    target_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()

    frozen = await client.post(
        f"/admin/wallets/{target_id}/freeze",
        headers=auth(tokens["access_token"]),
        json={"note": "Chargeback under investigation"},
    )
    assert frozen.status_code == 200
    assert frozen.json()["is_frozen"] is True

    thawed = await client.post(
        f"/admin/wallets/{target_id}/unfreeze", headers=auth(tokens["access_token"])
    )
    assert thawed.json()["is_frozen"] is False


async def test_reconcile_reports_a_clean_system(client, session):
    tokens = await register_and_verify(client, email="admin@example.com")
    await _make_admin(session, "admin@example.com")

    response = await client.post("/admin/reconcile", headers=auth(tokens["access_token"]))

    assert response.status_code == 200
    body = response.json()
    assert body["diverged"] == 0


async def test_the_signup_bonus_arrives_with_the_account(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "billing_signup_bonus_micros", 5 * CREDIT)
    tokens = await register_and_verify(client)

    body = (await client.get("/wallet", headers=auth(tokens["access_token"]))).json()

    assert body["bonus_micros"] == 5 * CREDIT
    assert body["available_micros"] == 5 * CREDIT
    assert body["bonus_expires_at"] is not None
