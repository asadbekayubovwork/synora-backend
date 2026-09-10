"""Opening, settling and abandoning a metered call.

`app/models/ai_session.py` argues that a one-shot request and a twenty-minute
voice call belong in one table. This is the other half of that argument: they
belong in one algorithm too. A one-shot call is the realtime loop with the loop
taken out — open, report, close — so it settles at `sequence=1`, its
`settled_micros` chases `estimated_micros` exactly as it does across three
hundred heartbeats, and its price comes from pricing the *cumulative*
quantities against a pinned price book rather than from trusting a delta
somebody sent us. Nothing below knows what text-to-speech is; STT and chat get
these same six functions.

Three of the six exist because of what a *streaming* one-shot turned out to be.
`touch_session` is there because a response body is paced by whoever is reading
it, so "past its deadline" and "not coming back" are different statements and
only the second one is safe to act on. `request_digest_for` is there because an
idempotency key means "this request again", and the price a request quotes to
cannot answer that — the price book buckets by the thousand characters, so a
thousand different requests price identically and a key checked against its
price is a key that unlocks any of them. And `settle_at_estimate` is there
because a stream that stalls before its first byte still cost us the whole
synthesis: the text was handed to the supplier before the hold was placed, so a
deadline that arrives with no report is a reason to bill rather than a reason to
stop billing, and the reaper that used to abandon those sessions at zero was
handing the work out free.

What one-shot adds is that the estimate is not an estimate. The whole input is
in the request body before any work starts — every character of the text — so
the price is known up front, the hold is the price rather than a guess at it,
and the settlement can never exceed what was held. That is the property the
rest of this module leans on, and it is why the hold is placed before a single
byte of work is done: a `402` belongs before the synthesis, not after it.

The one deliberate deviation from the obvious shape is at the settlement.
`wallet_repo.debit` will happily give a hold back in the same statement as the
charge, and doing it that way would be wrong here — see the long comment at
that call site. The hold is handed back first, in its own step inside the same
transaction, which turns "the money for this charge is there" from a hope into
a consequence of the wallet's own non-negativity invariant.

Everything here is idempotent, and not as a nicety. `settle_oneshot` is called
from the `finally` of a streaming response, and a `finally` runs more often
than whoever wrote it expects: a client disconnect, an exception on the way
out, a generator closed twice. So the first thing it does is ask whether the
session is already over, and every wallet movement it makes is keyed on the
session's own id.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PaymentRequiredError,
)
from app.db.base import utcnow
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    TERMINAL_SESSION_STATUSES,
    AiSessionKind,
    AiSessionStatus,
    BillingService,
    LedgerRefType,
    SessionEndReason,
    UsageEventKind,
    UsageEventStatus,
    UsageMetric,
)
from app.models.usage import UsageEvent, UsageEventItem
from app.services.billing import pricing, wallet_repo, wallet_service
from app.services.billing.pricing import PricedLine

logger = logging.getLogger("synora.billing")

# A one-shot session reports exactly once, so its single usage event is always
# sequence 1. Named rather than spelled `1` in five places, because
# `uq_usage_events_session_sequence` is what turns a second report into a
# conflict instead of a second charge.
ONESHOT_SEQUENCE = 1

# The longest key a *client* is allowed to send. Exported because the schema
# layer and the route layer both publish it in OpenAPI, and a published limit
# that is not the enforced one is worse than no published limit at all: a
# client that generates a key at exactly the documented length gets a 400
# naming a constraint that appears nowhere in the contract. One number, one
# place, three consumers.
MAX_CLIENT_IDEMPOTENCY_KEY = 128

# What we actually store: the client's key with `{user_id}:{scope}:` on the
# front of it — 37 characters of uuid and colon, plus the scope.
#
# Read off the mapped column rather than written down as a number, and that is
# the whole point of this line. The number written down here was 176, justified
# by a comment asserting `AiSession.idempotency_key` was String(200) when it was
# String(128) — String(200) belongs to a different table — so the guard below
# waved through a 172-character value into a column that could not hold it.
# Postgres answers that with 22001 and a 500 on the one path whose entire job is
# to make a retry safe; SQLite ignores the declared width and stores the whole
# string, so the suite stayed green and the drift was invisible for a release.
#
# A constant that merely agrees with the schema is a constant that will one day
# stop agreeing with it, and no test running on SQLite can ever be the thing
# that notices. This one cannot drift: it *is* the schema. The arithmetic that
# has to hold — prefix plus `MAX_CLIENT_IDEMPOTENCY_KEY` fits — is asserted in
# `tests/test_billing_invariants.py`, which is the only guard that works.
#
# Checked here rather than left to the database for the same reason it is
# derived here: an over-long value is a 500 on Postgres and a silent
# full-width store on SQLite, and the second one is worse — two "identical"
# keys differing by their tails is idempotency switched off with nobody told.
IDEMPOTENCY_KEY_MAX_LENGTH: int = AiSession.__table__.c.idempotency_key.type.length

# The reaper's backstop, not a deadline anybody waits on. It is what frees the
# hold when the process serving the request dies between placing it and
# settling. Ten minutes comfortably outlives the longest upstream read timeout
# and is short enough that stranded credit comes back within the hour. The
# reaper is `reconcile_service.reap_expired_sessions`, named here because this
# number means nothing without something that reads it — and for a while there
# was nothing, which is how a stuck hold became permanent rather than late.
#
# Ten minutes is emphatically *not* a bound on how long a streaming response may
# take: a `/tts/speech` body is client-paced, so a phone on a slow connection
# can legitimately still be reading at twenty. That is why the reaper does not
# act on this alone — it also asks when the session last made progress, and
# `touch_session` below is what answers. Passing this deadline means "started a
# while ago"; being reaped means "and nothing has happened since".
DEFAULT_ONESHOT_TTL_SECONDS = 600


# --- what the callers get back ---------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """An open session, and everything the caller needs to settle it.

    `replayed` says the caller sent an idempotency key we had already seen and
    this is the session that key already opened — no second hold was placed and
    no second charge will be made. Whether to redo the work is the caller's
    decision, not ours; settling twice is already safe.
    """

    ai_session_id: uuid.UUID
    user_id: uuid.UUID
    wallet_id: uuid.UUID
    service: BillingService
    model_key: str
    price_book_version_id: uuid.UUID
    reserved_micros: int
    estimated_micros: int
    replayed: bool


@dataclass(frozen=True)
class Quote:
    """A price with no session and no wallet behind it."""

    price_book_version_id: uuid.UUID
    price_micros: int
    cost_micros: int
    lines: tuple[PricedLine, ...]


@dataclass(frozen=True)
class Settlement:
    """What one settlement charged, and what it could not collect.

    `price_micros` is this event's own charge and `cumulative_price_micros` is
    the session total it brought us to. For a one-shot the two are equal; they
    are both here because the realtime path will return the same shape and the
    difference is the whole point there.
    """

    ai_session_id: uuid.UUID
    usage_event_id: uuid.UUID | None
    price_micros: int
    cumulative_price_micros: int
    debited_micros: int
    writeoff_micros: int
    cost_micros: int
    clamped: bool
    replayed: bool


# --- helpers ---------------------------------------------------------------


async def _load(session: AsyncSession, ai_session_id: uuid.UUID) -> AiSession:
    row = (
        await session.execute(select(AiSession).where(AiSession.id == ai_session_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("No such session.", code="ai_session_not_found")
    return row


def _ticket(row: AiSession, *, replayed: bool) -> Ticket:
    return Ticket(
        ai_session_id=row.id,
        user_id=row.user_id,
        wallet_id=row.wallet_id,
        service=row.service,
        model_key=row.model_key,
        price_book_version_id=row.price_book_version_id,
        reserved_micros=row.reserved_micros,
        estimated_micros=row.estimated_micros,
        replayed=replayed,
    )


def _finish(
    row: AiSession,
    *,
    status: AiSessionStatus,
    end_reason: SessionEndReason,
    now: datetime,
    error_code: str | None = None,
) -> None:
    """Stamp a terminal state, and stamp it the same way every time.

    `hold_released_at` is the idempotency guard for the release — non-null
    exactly once — so it is written by whatever makes the session terminal and
    never separately. It is stamped even on the path where no hold was ever
    placed: `reserved_micros` is zero there, so the reconciler's sum is right
    either way, but a null would leave the row sitting in
    `ix_ai_sessions_unreleased_holds` forever claiming to hold nothing.
    """
    row.status = status
    row.end_reason = end_reason
    row.error_code = error_code
    row.ended_at = now
    row.hold_released_at = now
    row.reserved_micros = 0
    # The compare-and-swap token the realtime path reads. A one-shot has no
    # contender for it, but leaving it unmoved would make "has this changed
    # since you looked?" unanswerable for the reaper.
    row.version = row.version + 1


# --- what a key was spent on -----------------------------------------------


# Not a colon or a comma: both occur in the values being fingerprinted, and a
# separator that can appear inside a field is not a separator. With a plain
# concatenation `("ab", "c")` and `("a", "bc")` are the same request, which is
# a collision an attacker picks rather than one they wait for.
_FIELD_SEPARATOR = "\x1f"
# A field that was not supplied, kept distinct from a field supplied empty.
# "no voice, use the default" and "the voice named empty-string" are different
# requests even when they render the same audio today.
_FIELD_ABSENT = "\x00"


def request_digest_for(*parts: str | int | None) -> str:
    """Fingerprint the request a session is being opened for.

    This exists because an idempotency key means "this request again", and
    until there was somewhere to write down what the request *was*, nothing
    could check that claim. The replay guard used to compare the price of the
    replay against the price the session was opened at, which cannot work: the
    price book charges per thousand characters with CEIL rounding, so every
    text from one character to a thousand quotes the same number. A price is a
    bucket, and a bucket is not an identity. One paid character bought five
    thousand free ones, and for a hundred-thousand-character original the
    bucket was every text between 99001 and 100000 characters.

    Callers hand over the fields that decide what gets synthesised — text,
    voice, quality, format, sample rate, style — positionally, in a fixed
    order. Positional and fixed is the trade: it costs each caller a rule to
    remember, and it buys a digest that is one line to compute and impossible
    to compute two different ways for the same surface. That is why this is a
    function here rather than a `sha256` at each call site, which is how two
    call sites end up hashing two different field lists and a replay stops
    matching itself.

    Not keyed and not salted. The comparison is ours against our own stored
    value, never against anything a client sends, so there is nothing here for
    an HMAC to protect: a preimage buys an attacker only the text they would
    have had to send anyway. What is required is that two different requests
    cannot share a digest, and sha256 is the cheap end of that guarantee.

    Sixty-four hex characters, which is exactly the width of
    `AiSession.request_digest`; the invariant test pins the two together.
    """
    joined = _FIELD_SEPARATOR.join(
        _FIELD_ABSENT if part is None else str(part) for part in parts
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --- pricing ---------------------------------------------------------------


async def quote(
    session: AsyncSession,
    *,
    service: BillingService,
    model_key: str,
    quantities: Mapping[UsageMetric, int],
) -> Quote:
    """Price something without touching a wallet.

    The estimate endpoint and `open_oneshot` both come through here, so the
    number a caller is shown and the number they are held for are produced by
    one piece of code. A quote that can disagree with the charge is worse than
    no quote at all.
    """
    book = await pricing.active_price_book(session)
    prices = await pricing.prices_for(
        session,
        price_book_version_id=book.id,
        service=service,
        model_key=model_key,
    )
    priced = pricing.price_cumulative(
        dict(quantities), prices, price_book_version_id=book.id
    )
    return Quote(
        price_book_version_id=book.id,
        price_micros=priced.price_micros,
        cost_micros=priced.cost_micros,
        lines=priced.lines,
    )


# --- opening ---------------------------------------------------------------


async def _replayed_ticket(session: AsyncSession, idempotency_key: str) -> Ticket:
    row = (
        await session.execute(
            select(AiSession).where(AiSession.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if row is None:  # pragma: no cover - would mean a different constraint fired
        raise ConflictError(
            "This session could not be opened. Please retry.",
            code="ai_session_conflict",
        )
    return _ticket(row, replayed=True)


async def open_oneshot(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    service: BillingService,
    model_key: str,
    quantities: Mapping[UsageMetric, int],
    scope: str,
    idempotency_key: str | None = None,
    request_digest: str | None = None,
    ttl_seconds: int = DEFAULT_ONESHOT_TTL_SECONDS,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> Ticket:
    """Price the work, hold the credit for it, and hand back a ticket.

    Owns its commit, so the session row and its hold are durable before the
    caller starts the upstream request. That ordering is the point: if this
    process dies mid-stream, what survives is a session that visibly held
    credit and never settled, which the reaper can finish. The reverse ordering
    leaves work done and nothing to bill it against.

    `scope` names the surface the key belongs to — `speech`, `batch` — and is
    required rather than defaulted, because the default anybody would pick is
    the one that reintroduces the collision it exists to prevent.

    `request_digest` is what makes a replay answerable. Built with
    `request_digest_for`, it fingerprints the request this session was opened
    for, so a later call under the same key can be asked "is this the same
    request?" rather than the question that used to be asked, "does it cost the
    same?" — which the price book's thousand-character rounding answers `yes`
    to for a thousand different requests. Optional, because a caller with its
    own record of what a key bought does not need it; a null is stored as a
    null and a replay guard reading one has to refuse, since "we wrote nothing
    down" is not evidence that this is the same request.
    """
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    # Before the price book and before the row: a frozen wallet is a 403, and
    # there is no reason to quote work it cannot pay for.
    wallet_repo.require_unfrozen(snapshot)

    quoted = await quote(
        session, service=service, model_key=model_key, quantities=quantities
    )

    if idempotency_key and len(idempotency_key) > MAX_CLIENT_IDEMPOTENCY_KEY:
        # Checked against the client's own share of the key, so the number in
        # the message is the number the schema published.
        raise BadRequestError(
            f"An idempotency key may be at most {MAX_CLIENT_IDEMPOTENCY_KEY} "
            "characters.",
            code="idempotency_key_too_long",
        )

    # Scoped to the user, because `uq_ai_sessions_idempotency_key` is global:
    # unscoped, one client's "retry-1" collides with another's and the loser is
    # handed a stranger's session.
    #
    # Scoped to the *surface* as well, because the constraint cannot tell one
    # kind of call from another. With only the user on the front, a
    # one-character `POST /tts/speech` sent under the key of a running batch
    # resolves to the batch's session: it settles that session for one
    # character and hands back the batch's entire hold, so half a million
    # characters of GPU time end up billed at a quarter of a credit and the
    # batch's own settlement later finds nothing left to charge. A key means
    # "this request again", and two different routes are never the same
    # request.
    #
    # A caller with no key of its own gets a minted one rather than a null,
    # because a null deduplicates nothing.
    key = (
        f"{user_id}:{scope}:{idempotency_key}"
        if idempotency_key
        else f"{user_id}:{scope}:auto:{uuid.uuid4().hex}"
    )
    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        # Only reachable through a scope longer than thirty-four characters —
        # 36 of uuid, two colons and 128 of client key leave that much of the
        # column — which is our mistake rather than the caller's, since their
        # share was bounded above. Kept as a check rather than an assertion
        # because the consequence on Postgres is a 500 and on SQLite is worse.
        raise BadRequestError(
            "This idempotency key is too long.", code="idempotency_key_too_long"
        )

    now = utcnow()
    row = AiSession(
        user_id=user_id,
        wallet_id=snapshot.wallet_id,
        service=service,
        kind=AiSessionKind.ONESHOT,
        status=AiSessionStatus.PENDING,
        model_key=model_key,
        # Pinned now and never re-resolved. A price book published while the
        # stream is running must not change the price of the stream.
        price_book_version_id=quoted.price_book_version_id,
        estimated_micros=quoted.price_micros,
        # Unused by the proxy model — we are the microservice here, so nobody
        # redeems this — but the column is non-null and the uniqueness of the
        # value is what the internal `authorize` path relies on, so it is
        # minted properly rather than stubbed.
        authorize_jti=uuid.uuid4().hex,
        idempotency_key=key,
        request_digest=request_digest,
        expires_at=now + timedelta(seconds=ttl_seconds),
        client_ip=client_ip,
        user_agent=user_agent,
    )
    session.add(row)
    try:
        # The sessionmaker is `autoflush=False`, so without this the collision
        # below would surface at commit — where it is an unhandleable 500
        # rather than something recognisable as a replay.
        await session.flush()
    except IntegrityError:
        # The caller retried with a key we have seen. Roll back to before the
        # insert; nothing has moved yet, because the hold is placed below.
        await session.rollback()
        return await _replayed_ticket(session, key)

    try:
        movement = await wallet_repo.place_hold(
            session,
            wallet_id=snapshot.wallet_id,
            amount_micros=quoted.price_micros,
            # Derived from a session id minted microseconds ago, so this key is
            # new by construction and `place_hold`'s own replay branch can only
            # fire on a genuine retry against this same session.
            idempotency_key=f"hold:{row.id}",
            ai_session_id=row.id,
            note=f"{service.value} {model_key}",
        )
    except PaymentRequiredError as exc:
        # A 402 must not leave a `pending` row behind. The reaper would later
        # expire a session that never started, and anyone reading the table
        # could not tell "declined" from "in flight". The commit is the whole
        # point of this branch: the row has to survive the exception that is
        # about to unwind the request.
        _finish(
            row,
            status=AiSessionStatus.FAILED,
            end_reason=SessionEndReason.INSUFFICIENT_CREDIT,
            error_code=exc.code,
            now=utcnow(),
        )
        await session.commit()
        raise

    # `reserved_delta_micros` rather than the quote: it is what the wallet
    # actually moved, and for a free call `place_hold` reserves nothing at all.
    row.reserved_micros = movement.reserved_delta_micros
    row.hold_peak_micros = movement.reserved_delta_micros
    # Claimed in the same breath as opened. In the proxy model there is no
    # second party to hand the ticket to — we are the one doing the work — so a
    # `pending` row here would only ever be a row we crashed on.
    row.status = AiSessionStatus.ACTIVE
    row.claimed_at = now
    row.version = row.version + 1
    await session.commit()

    logger.info(
        "oneshot_open session=%s user=%s service=%s model=%s price=%s hold=%s",
        row.id,
        user_id,
        service.value,
        model_key,
        quoted.price_micros,
        movement.reserved_delta_micros,
    )
    return _ticket(row, replayed=False)


# --- staying alive ---------------------------------------------------------


async def touch_session(session: AsyncSession, *, ai_session_id: uuid.UUID) -> None:
    """Record that a live session is still making progress.

    `expires_at` is a deadline on *starting*, not on finishing, and for a
    streaming response those are not the same question. A `/tts/speech` body is
    client-paced: the upstream read only happens when the generator is pulled,
    so a mobile client on a throttled connection, or a paused `<audio>`
    element, legitimately holds a session open well past a TTL sized for a
    healthy stream, without ever tripping a read timeout. A reaper that acted
    on the deadline alone closed that session, released its hold, and stamped
    it `FAILED` — and the stream then finished normally into a session already
    terminal, so the settlement rebuilt a zero and the whole synthesis was
    free, with no record it had ever been billable. This is the half of the fix
    that produces the evidence; `reconcile_service.reap_expired_sessions` is
    the half that reads it. Together they make the deadline mean "no progress"
    rather than "started a while ago".

    A bulk `UPDATE` rather than a loaded row: this is called as bytes move, the
    row itself is not wanted, and one statement against the primary key is as
    cheap as the write gets. Conditional on the session not being terminal,
    because the last chunk and the settlement's `finally` are microseconds
    apart — a touch that lands after the close must not stamp a fresh heartbeat
    onto a settled row, which would make a finished session read as live to
    every query in `reconcile_service`.

    `updated_at` is set by hand. `Base.updated_at`'s `onupdate` is applied by
    SQLAlchemy in Python and does not fire on a bulk update, so leaving it out
    would freeze the column at the row's creation time and quietly mislead
    anything that orders by it.

    Owns its commit, as everything else in this module does. The reaper runs in
    another process against another connection: a heartbeat it cannot see is
    not a heartbeat. There is nothing here to roll back and nothing here worth
    holding a transaction open for.
    """
    now = utcnow()
    await session.execute(
        update(AiSession)
        .where(
            AiSession.id == ai_session_id,
            AiSession.status.not_in(TERMINAL_SESSION_STATUSES),
        )
        .values(
            last_heartbeat_at=now,
            # Counted as well as stamped. "How far did this stream actually
            # get" is the first question anyone asks of a session that was
            # reaped anyway, and a timestamp alone cannot answer it.
            heartbeat_count=AiSession.heartbeat_count + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


# --- settling --------------------------------------------------------------


async def _rebuild_settlement(session: AsyncSession, row: AiSession) -> Settlement:
    """The answer we gave the first time, rebuilt from what we wrote down.

    Reconstructed rather than cached, for the reason `wallet_repo._replay`
    reconstructs: the stored row is the thing that cannot lie. A terminal
    session with no event is one that was abandoned before any work was
    delivered, and settling it to zero is the truthful answer rather than an
    error — the caller asking twice is not doing anything wrong.
    """
    event = (
        await session.execute(
            select(UsageEvent).where(
                UsageEvent.ai_session_id == row.id,
                UsageEvent.sequence == ONESHOT_SEQUENCE,
            )
        )
    ).scalar_one_or_none()

    if event is None:
        return Settlement(
            ai_session_id=row.id,
            usage_event_id=None,
            price_micros=0,
            cumulative_price_micros=row.settled_micros,
            debited_micros=0,
            writeoff_micros=row.writeoff_micros,
            cost_micros=row.cost_micros,
            clamped=False,
            replayed=True,
        )

    return Settlement(
        ai_session_id=row.id,
        usage_event_id=event.id,
        price_micros=event.price_micros,
        cumulative_price_micros=event.cumulative_price_micros,
        debited_micros=event.debited_micros,
        writeoff_micros=event.writeoff_micros,
        cost_micros=event.cost_micros,
        clamped=event.clamped,
        replayed=True,
    )


async def _collect(
    session: AsyncSession,
    *,
    row: AiSession,
    event: UsageEvent,
    charge: int,
) -> wallet_repo.WalletMovement:
    """Charge for a settlement whose hold has already been handed back.

    Past the release there is no way back, and no way to stop half way. The
    release, the usage event and the terminal stamp share one transaction with
    this charge, so an exception raised here does not leave a partly settled
    session — it undoes all four together, and what is left is an ACTIVE
    session holding credit that nothing will ever come back for. Worse, it is
    stable: every later attempt reaches the same failing `debit` and rolls the
    same work back again, and the reconciler counts an ACTIVE session's hold as
    legitimately held, so nothing even reports it.

    Hence the principle this function exists to enforce: **the released hold is
    proof the credit was committed to this session, so a settlement that has
    got this far must reach a terminal state whatever the balance now says.**

    Two things can still refuse the charge. A bonus bucket that lapsed while a
    six-hour batch was running drops `available` below the charge and `debit`
    answers `PaymentRequiredError` — the credit was genuinely there when the
    hold was placed, and the user did the work we are billing for. A wallet
    under contention exhausts `MAX_CAS_ATTEMPTS` and answers `wallet_busy`.
    Both get exactly one more attempt, with the whole charge allowed as
    write-off: that collects whatever is actually in the wallet and records the
    remainder on `wallets.lifetime_writeoff_micros` and
    `UsageEvent.writeoff_micros`, which is the honest account of what happened.
    `disputed` rides along, because a write-off of this shape is not the
    ordinary grace path and support has to be able to find it.
    """

    async def _debit(max_writeoff_micros: int) -> wallet_repo.WalletMovement:
        return await wallet_repo.debit(
            session,
            wallet_id=row.wallet_id,
            amount_micros=charge,
            # The same key on both attempts. The first one wrote nothing, so
            # `debit`'s replay branch cannot fire on the second; and if some
            # concurrent settlement did write under it, we are handed their
            # movement instead of charging a second time.
            idempotency_key=f"usage:{event.id}",
            ref_type=LedgerRefType.USAGE_EVENT,
            ai_session_id=row.id,
            usage_event_id=event.id,
            # Already released above; passing it again would double-count.
            release_reserved_micros=0,
            max_writeoff_micros=max_writeoff_micros,
            note=f"{row.service.value} {row.model_key}",
        )

    try:
        return await _debit(settings.billing_grace_micros)
    except (PaymentRequiredError, ConflictError) as error:
        if isinstance(error, ConflictError) and error.code != "wallet_busy":
            # `billing_write_conflict` is raised out of `_flush_ledger`, which
            # has already rolled the whole transaction back — the usage event
            # this charge points at no longer exists, so there is nothing left
            # here to retry against. The reaper is the backstop for that one.
            raise
        logger.error(
            "oneshot_settle_forced session=%s wallet=%s charge=%s after=%s",
            row.id,
            row.wallet_id,
            charge,
            error.code,
        )
        row.disputed = True
        return await _debit(charge)


async def settle_oneshot(
    session: AsyncSession,
    *,
    ai_session_id: uuid.UUID,
    quantities: Mapping[UsageMetric, int],
    end_reason: SessionEndReason,
    upstream_request_id: str | None = None,
) -> Settlement:
    """Charge for the work, release the hold, close the session.

    Callers report **cumulative** quantities and only metrics the price book
    covers — see the comment on `price_cumulative` below, which is the one way
    to make this function raise where nobody can catch it.
    """
    row = await _load(session, ai_session_id)

    # First, before a price is read or a wallet is touched. This runs in the
    # `finally` of a streaming response, so the second call is the normal case
    # rather than the exotic one: the client disconnected, the generator was
    # closed twice, the route retried after the status line had already gone
    # out. The only safe answer to the second call is the first call's answer.
    if row.status in TERMINAL_SESSION_STATUSES:
        return await _rebuild_settlement(session, row)

    now = utcnow()
    previous = {metric: row.cumulative(metric) for metric in UsageMetric}
    for metric, quantity in quantities.items():
        # `stored = max(stored, incoming)`, the rule `usage.py` sets out. A
        # one-shot reports once, so the max is never the interesting branch
        # here; it is here because the realtime path shares this code and
        # because a total that went backwards is a bug we decline to bill.
        setattr(row, metric.cumulative_column, max(previous[metric], int(quantity)))

    prices = await pricing.prices_for(
        session,
        # The book pinned at open, not today's.
        price_book_version_id=row.price_book_version_id,
        service=row.service,
        model_key=row.model_key,
    )
    # `price_cumulative` raises `BadRequestError` for any metric carrying a
    # quantity above zero that has no price row, and `cumulative_quantities()`
    # hands it every `cum_*` column above zero. So a caller may only ever
    # report metrics the price book covers: for TTS that is `tts_characters`
    # and nothing else. Filling in `cum_tts_audio_ms` "for the dashboard" turns
    # every settlement into a 400 raised out of a `finally`, where the response
    # has already been sent and nobody is left to catch it. Audio duration
    # belongs in a response header and a log line, not in a priced column.
    priced = pricing.price_cumulative(
        row.cumulative_quantities(),
        prices,
        price_book_version_id=row.price_book_version_id,
    )

    # The ceiling is what we quoted or what we held, whichever was larger.
    # Above it nothing extra is charged and the row is flagged: an over-report
    # is either an upstream bug or a bill for work nobody authorised, and both
    # are questions for a human rather than a silent charge.
    ceiling = max(row.estimated_micros, row.reserved_micros)
    clamped = priced.price_micros > ceiling
    cumulative_price = min(priced.price_micros, ceiling)
    # `settled_micros` is zero for a one-shot. Subtracting it anyway is what
    # makes this the same arithmetic the heartbeat path runs, and it is the
    # reason rounding happens once per session instead of once per report.
    charge = max(0, cumulative_price - row.settled_micros)

    if clamped:
        logger.warning(
            "oneshot_clamped session=%s priced=%s ceiling=%s reason=%s",
            row.id,
            priced.price_micros,
            ceiling,
            end_reason.value,
        )

    event = UsageEvent(
        ai_session_id=row.id,
        user_id=row.user_id,
        wallet_id=row.wallet_id,
        service=row.service,
        model_key=row.model_key,
        price_book_version_id=row.price_book_version_id,
        kind=UsageEventKind.ONESHOT,
        status=UsageEventStatus.RECORDED,
        sequence=ONESHOT_SEQUENCE,
        # When the work happened, as against `created_at` — when we stored it.
        occurred_at=now,
        price_micros=charge,
        cost_micros=priced.cost_micros,
        cumulative_price_micros=cumulative_price,
        clamped=clamped,
        # Globally unique without a prefix scheme, because the session id is.
        idempotency_key=f"oneshot:{row.id}",
        upstream_request_id=upstream_request_id,
    )
    session.add(event)
    try:
        await session.flush()
    except IntegrityError:
        # Two settlements racing from two different database sessions. The
        # terminal check at the top is a read, not a lock;
        # `uq_usage_events_session_sequence` is the lock. Whoever inserted
        # first owns the charge, and we hand back what they wrote.
        await session.rollback()
        return await _rebuild_settlement(session, await _load(session, ai_session_id))

    for line in priced.lines:
        session.add(
            UsageEventItem(
                usage_event_id=event.id,
                metric=line.metric,
                # The delta this event added. Identical to the cumulative for a
                # one-shot, computed rather than assumed because the realtime
                # path fills the same column with a real delta and the two have
                # to mean the same thing.
                quantity=line.quantity - previous[line.metric],
                cumulative_quantity=line.quantity,
                price_id=line.price_id,
                unit_size=line.unit_size,
                price_micros_per_unit=line.price_micros_per_unit,
                rounding=line.rounding,
                # A clamped total leaves these lines carrying what they priced
                # to rather than a share of what was charged. Scaling them down
                # would invent numbers no price row produces; `clamped` on the
                # event is the honest record of the difference.
                price_micros=line.price_micros,
                cumulative_price_micros=line.price_micros,
                cost_micros=line.cost_micros,
            )
        )
    await session.flush()

    # Hand the hold back *before* charging, rather than letting the charge do
    # it in one statement via `release_reserved_micros`.
    #
    # `wallet_repo.debit` takes `chargeable = min(amount, available)`, and
    # `available` is net of `reserved` — which still contains this session's
    # own hold, because the release rides on the same UPDATE that reads the
    # balance and therefore lands after the check. Charging against a hold that
    # is still in place means the wallet has to cover the charge *twice* over:
    # anything above `balance - hold` falls into the grace write-off and is
    # given away. A user whose balance is merely under twice the price of the
    # call would get the difference free, every call, for as long as it stayed
    # that way.
    #
    # Releasing first makes `available >= charge` a theorem rather than a hope.
    # `place_hold` never lets `paid + bonus - reserved` go negative, so the
    # balance covers every outstanding hold; once ours is back, the money it
    # was standing for is provably there, and the settled amount is at most the
    # hold because full-text billing prices the whole input up front. The two
    # statements share one transaction and one commit, so no other writer ever
    # observes the gap between them.
    #
    # This is the line that breaks the day somebody bills per *delivered*
    # character instead of per submitted one: the settlement would stop being
    # bounded by the hold, and `billing_grace_micros` below would start
    # absorbing ordinary revenue instead of genuine overruns.
    if row.reserved_micros:
        await wallet_repo.release_hold(
            session,
            wallet_id=row.wallet_id,
            amount_micros=row.reserved_micros,
            # Shared with `abandon_oneshot` deliberately: whichever of the two
            # runs first gives the hold back, and the other replays that answer
            # instead of releasing the same credit twice.
            idempotency_key=f"release:{row.id}",
            ai_session_id=row.id,
            note="Settled one-shot session",
        )

    debited = 0
    writeoff = 0
    ledger_group_id = None
    if charge:
        # Never raises for a wallet that merely cannot pay; see `_collect`.
        movement = await _collect(session, row=row, event=event, charge=charge)
        debited = movement.charged_micros
        writeoff = movement.writeoff_micros
        ledger_group_id = movement.group_id
    # A zero charge moves nothing, so it writes no ledger entry and leaves
    # `ledger_group_id` null — which is exactly what that column documents.

    event.debited_micros = debited
    event.writeoff_micros = writeoff
    event.ledger_group_id = ledger_group_id

    row.settled_micros = cumulative_price
    # The estimate becomes the actual. Keeping the two equal is what makes
    # `settled <= estimated` readable as an invariant rather than as an
    # accident, and a clamped session records its excess on the event.
    row.estimated_micros = cumulative_price
    row.writeoff_micros = writeoff
    row.cost_micros = priced.cost_micros
    # `or`, not `=`: `_collect` may already have flagged a forced write-off,
    # and an unclamped price must not clear it again.
    row.disputed = row.disputed or clamped
    # One report, so at least one heartbeat. Counting it keeps a one-shot row
    # and a realtime row comparable in the same query. `max`, not `=`, because
    # `touch_session` bumps this as bytes move: overwriting a real progress
    # count with 1 would erase the only record of how far the stream got,
    # which is the number anyone investigating a reaped session wants first.
    row.heartbeat_count = max(row.heartbeat_count, ONESHOT_SEQUENCE)
    row.last_heartbeat_at = now
    row.last_sequence = ONESHOT_SEQUENCE
    # `CLOSED` regardless of *why* the stream ended: the session settled, and
    # that is what `closed` means. `end_reason` carries "the client hung up".
    # `FAILED` is reserved for sessions that charged nothing at all.
    _finish(row, status=AiSessionStatus.CLOSED, end_reason=end_reason, now=now)
    await session.commit()

    logger.info(
        "oneshot_settled session=%s charge=%s debited=%s writeoff=%s clamped=%s reason=%s",
        row.id,
        charge,
        debited,
        writeoff,
        clamped,
        end_reason.value,
    )
    # After the commit, never before it. A counter incremented next to a write
    # that then rolls back is revenue on a dashboard that no wallet ever paid,
    # and the two would drift apart in the one direction nobody audits — the
    # graph reading high. Every replay path above returns before this line, so
    # a retried settlement is counted once, by whoever actually charged it.
    metrics.record_settlement(
        service=row.service.value,
        end_reason=end_reason.value,
        debited_micros=debited,
        writeoff_micros=writeoff,
        clamped=clamped,
    )
    return Settlement(
        ai_session_id=row.id,
        usage_event_id=event.id,
        price_micros=charge,
        cumulative_price_micros=cumulative_price,
        debited_micros=debited,
        writeoff_micros=writeoff,
        cost_micros=priced.cost_micros,
        clamped=clamped,
        replayed=False,
    )


# --- settling on a deadline instead of a report ----------------------------


async def settle_at_estimate(
    session: AsyncSession,
    *,
    ai_session_id: uuid.UUID,
    end_reason: SessionEndReason,
    error_code: str | None = None,
) -> Settlement:
    """Charge a claimed session the price it was opened at, with no report.

    For the one case where work was certainly committed and no report is ever
    going to arrive: a session that was claimed, ran past its deadline and went
    silent. The alternative — the one this replaces — was to abandon it at
    zero, and that gave synthesis away. This product bills the whole submitted
    text, and it bills it precisely because by the time a hold exists the
    entire text has already been handed to the supplier; that is the settled
    policy, and it is the reason the price is knowable before the first byte
    goes out. A client that opens `POST /tts/speech`, reads the headers and
    never pulls a byte reaches `expires_at` with `last_heartbeat_at` still
    null, is reaped, and then drains the body into a session already terminal —
    `settle_oneshot` sees the terminal status, rebuilds a zero, and a thousand
    characters are delivered unpaid. Reaping that row is right. Charging
    nothing for it is not.

    **Why the estimate rather than a price computed from quantities.**
    `settle_oneshot` prices *cumulative quantities*, so the obvious shape here
    is to hand it the quantities the session was opened with — and `AiSession`
    does not keep them. The `cum_*` columns mean "reported so far", and
    pre-filling them at open to make them available here would make a session
    that never started read as fully consumed, and would collapse the item
    delta every healthy settlement writes (`quantity = incoming - stored`) to
    zero. What the row does keep is `estimated_micros`, which for a one-shot is
    not an estimate at all: the whole input is in the request body before any
    work begins, so `open_oneshot` holds the exact price of the text and
    `settled == estimated` is what an ordinary healthy call settles to. Billing
    the estimate is therefore billing the number a report would have produced,
    arrived at from the other end — and it needs neither the quantities nor the
    price book, which matters, because a reaper that depended on the pinned
    book still resolving would fail on exactly the oldest sessions.

    **The trade, stated plainly.** This charges for work we believe was
    delivered rather than work we watched being delivered, so it will sometimes
    bill for a synthesis nobody received. That is deliberate, and it is
    survivable in one direction only: reversing it on appeal is a refund, which
    the ledger already supports and which leaves an entry behind rather than
    erasing one. Under-charging to zero leaves nothing at all — no event, no
    ledger row, no number for anyone to notice or argue with. Correctable and
    visible beats silent and permanent. So the row is stamped `disputed`
    whatever the arithmetic did, and support can list every call billed on a
    deadline rather than on a report with one predicate.

    **No usage event items.** An item asserts "this many units at this rate",
    and here we have neither; deriving a quantity back out of a total is
    fabrication, and the price book's CEIL rounding does not invert anyway. So
    the event carries the money and no line carries a quantity, which means
    `GET /usage` — the authority on consumption, as against the statement,
    which is the authority on money — under-reports these calls by exactly the
    consumption nobody ever reported. `cost_micros` stays zero for the same
    reason: we do not know what the supplier billed us for it.

    Idempotent like everything else in this module, and for the same reason:
    a reap and a late settlement can arrive in either order, and whichever is
    second has to be given the first one's answer rather than a second charge.
    """
    row = await _load(session, ai_session_id)
    if row.status in TERMINAL_SESSION_STATUSES:
        return await _rebuild_settlement(session, row)

    # `- settled_micros` for the reason `settle_oneshot` subtracts it: the
    # amount owed is the cumulative price less what has already been collected.
    # It is zero for every session that reaches here — one that had settled
    # would be terminal — and writing the arithmetic the same way in both
    # places is what keeps "settled never exceeds estimated" checkable by eye.
    cumulative_price = row.estimated_micros
    charge = max(0, cumulative_price - row.settled_micros)
    # There is no clamp branch and none is possible: `settle_oneshot`'s ceiling
    # is `max(estimated, reserved)`, and the number billed here *is* the
    # estimate, so it cannot come out above its own ceiling.

    if charge <= 0:
        # A free call, or a session whose estimate was never written. Nothing
        # is owed, and a zero-price `UsageEvent` is a row asserting a bill that
        # does not exist. This module's rule is that `FAILED` is for sessions
        # that charged nothing at all and `abandon_oneshot` is the function
        # that writes that row, so the empty case goes through it rather than
        # growing a second way of saying the same thing.
        settled_before = row.settled_micros
        writeoff_before = row.writeoff_micros
        cost_before = row.cost_micros
        await abandon_oneshot(
            session,
            ai_session_id=ai_session_id,
            end_reason=end_reason,
            error_code=error_code,
        )
        return Settlement(
            ai_session_id=ai_session_id,
            usage_event_id=None,
            price_micros=0,
            cumulative_price_micros=settled_before,
            debited_micros=0,
            writeoff_micros=writeoff_before,
            cost_micros=cost_before,
            clamped=False,
            replayed=False,
        )

    now = utcnow()
    event = UsageEvent(
        ai_session_id=row.id,
        user_id=row.user_id,
        wallet_id=row.wallet_id,
        service=row.service,
        model_key=row.model_key,
        price_book_version_id=row.price_book_version_id,
        kind=UsageEventKind.ONESHOT,
        status=UsageEventStatus.RECORDED,
        sequence=ONESHOT_SEQUENCE,
        # The deadline is the only handle we have on when the work happened;
        # `created_at` records when we gave up waiting to be told about it.
        occurred_at=now,
        price_micros=charge,
        cost_micros=0,
        cumulative_price_micros=cumulative_price,
        clamped=False,
        # The key `settle_oneshot` would have used, deliberately. If a real
        # report lands from another connection at this moment, one of the two
        # loses `uq_usage_events_session_sequence` and replays the winner's
        # answer instead of charging the session twice.
        idempotency_key=f"oneshot:{row.id}",
    )
    session.add(event)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return await _rebuild_settlement(session, await _load(session, ai_session_id))

    # Released before the charge, for the reason set out at length in
    # `settle_oneshot`: `debit` computes `available` net of `reserved`, so
    # charging against a hold that is still in place makes the wallet cover the
    # same money twice and hands the difference away as write-off. Same key as
    # the other two terminal paths, so whichever of them ran first is replayed
    # rather than releasing the same credit again.
    if row.reserved_micros:
        await wallet_repo.release_hold(
            session,
            wallet_id=row.wallet_id,
            amount_micros=row.reserved_micros,
            idempotency_key=f"release:{row.id}",
            ai_session_id=row.id,
            note="Settled one-shot session at its estimate",
        )

    # Never raises for a wallet that merely cannot pay; see `_collect`. That
    # matters more here than on the ordinary path, because this runs inside a
    # reconciliation loop that may be holding hundreds of other frozen holds
    # behind it.
    movement = await _collect(session, row=row, event=event, charge=charge)
    event.debited_micros = movement.charged_micros
    event.writeoff_micros = movement.writeoff_micros
    event.ledger_group_id = movement.group_id

    row.settled_micros = cumulative_price
    row.writeoff_micros = movement.writeoff_micros
    row.last_sequence = ONESHOT_SEQUENCE
    # `last_heartbeat_at` and `heartbeat_count` are left exactly as they were,
    # which is the one place this deviates from `settle_oneshot`. They are the
    # evidence of why this row is here at all — a null heartbeat says the
    # stream never reported a single byte of progress — and stamping them now
    # would overwrite the only record of that with the moment we billed it.
    #
    # Unconditional, unlike `settle_oneshot`'s `or clamped`: a charge raised on
    # a deadline instead of on a report is always reviewable, whatever the
    # arithmetic did, and this flag is how support finds all of them.
    row.disputed = True
    # `CLOSED`, because the session settled and that is what closed means;
    # `end_reason` carries the fact that a deadline is what ended it, and
    # `error_code` carries which pass decided so.
    _finish(
        row,
        status=AiSessionStatus.CLOSED,
        end_reason=end_reason,
        error_code=error_code,
        now=now,
    )
    await session.commit()

    # WARNING, where `settle_oneshot` logs INFO. Money moved for work nobody
    # ever reported, which is never routine and is always somebody's bug.
    logger.warning(
        "oneshot_settled_at_estimate session=%s charge=%s debited=%s "
        "writeoff=%s reason=%s",
        row.id,
        charge,
        movement.charged_micros,
        movement.writeoff_micros,
        end_reason.value,
    )
    # Counted under the same metric as an ordinary settlement, told apart by
    # `end_reason` — `heartbeat_timeout` and `timeout` are the deadline
    # charges, and a dashboard that wants only the honest ones filters on the
    # label rather than needing a metric of its own.
    metrics.record_settlement(
        service=row.service.value,
        end_reason=end_reason.value,
        debited_micros=movement.charged_micros,
        writeoff_micros=movement.writeoff_micros,
        clamped=False,
    )
    return Settlement(
        ai_session_id=row.id,
        usage_event_id=event.id,
        price_micros=charge,
        cumulative_price_micros=cumulative_price,
        debited_micros=movement.charged_micros,
        writeoff_micros=movement.writeoff_micros,
        cost_micros=0,
        clamped=False,
        replayed=False,
    )


# --- giving up -------------------------------------------------------------


async def abandon_oneshot(
    session: AsyncSession,
    *,
    ai_session_id: uuid.UUID,
    end_reason: SessionEndReason,
    error_code: str | None,
    status: AiSessionStatus = AiSessionStatus.FAILED,
) -> None:
    """Give the hold back and charge nothing.

    For the case the streaming path cares about most: upstream failed before a
    single byte reached us, so there is no delivered work to bill for and the
    user should not pay for our supplier's bad afternoon. Called from the same
    `finally` as `settle_oneshot`, so it is as forgiving — a session somebody
    already closed is not an error here, it is that `finally` running twice.

    `status` defaults to `FAILED`, which is what "we gave up on a call we had
    claimed" means. The reaper passes `EXPIRED` for a session nobody ever
    claimed, because that is the distinction the enum reserves the member for
    and it is the first thing anyone reading the table wants to know: did the
    work start and die, or did the ticket never get taken up at all. It must
    stay inside `TERMINAL_SESSION_STATUSES` — a non-terminal value here would
    release the hold and leave the row looking live, which is the exact state
    this whole module exists to make impossible.
    """
    row = await _load(session, ai_session_id)
    if row.status in TERMINAL_SESSION_STATUSES:
        return

    now = utcnow()
    if row.reserved_micros:
        await wallet_repo.release_hold(
            session,
            wallet_id=row.wallet_id,
            amount_micros=row.reserved_micros,
            idempotency_key=f"release:{row.id}",
            ai_session_id=row.id,
            note="Abandoned one-shot session",
        )
    _finish(
        row,
        status=status,
        end_reason=end_reason,
        error_code=error_code,
        now=now,
    )
    await session.commit()

    metrics.record_abandon(service=row.service.value, end_reason=end_reason.value)

    logger.info(
        "oneshot_abandoned session=%s reason=%s error=%s",
        row.id,
        end_reason.value,
        error_code or "-",
    )
