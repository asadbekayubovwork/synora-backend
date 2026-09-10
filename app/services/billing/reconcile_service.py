"""Checking that the wallets still agree with the ledger, in production.

The invariant is enforced structurally — one writer, one transaction, an
append-only table — but "enforced" and "verified" are different words. This is
the verification, and it runs on a schedule rather than only in CI, because
the failure it is looking for is the kind that arrives with a code change
nobody thought was risky.

Three passes, and it is worth keeping them apart:

- **Sessions past their deadline.** `AiSession.expires_at` is documented as a
  hard stop "enforced by the reaper", and for a long time there was no reaper:
  every way a metered call can die without settling — a client that
  disconnected before the response body was started, a settlement that failed
  on a dead connection, a batch upstream never accepted, a process killed
  mid-stream — ended in a session stuck `ACTIVE` with its hold in place. That
  state is invisible to the other two passes, because a live session's hold is
  *supposed* to be held, so the drift below reads zero and nothing complains
  while the customer's credit stays frozen. `reap_expired_sessions` is the
  floor under all of them — and it is a floor, not a ceiling: a deadline says
  when a call *started*, so this pass also asks whether the session has since
  gone quiet, and stands off entirely from any session a live batch job still
  owns. What it does when it *does* act depends on whether the work ever
  started: a ticket nobody claimed is abandoned at zero, and a session that was
  claimed is settled at the price it was opened for. Closing a call that is
  still running used to be much the more expensive mistake, because the work
  was delivered either way and only the charge disappeared — that is the hole
  `session_service.settle_at_estimate` closes.
- **Reserved credit against the sessions holding it.** A mismatch here is
  almost always a leaked hold — a session that ended without releasing — and
  that one *is* auto-healed, because its effect is to freeze a paying
  customer's money for no reason, and the correct value is recomputable from
  the sessions themselves.
- **Balances against the ledger.** A mismatch here means credit appeared or
  vanished. Nothing repairs it automatically: a wrong balance is a question
  for a human, and silently rewriting it would destroy the evidence.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc, utcnow
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    TERMINAL_BATCH_STATES,
    TERMINAL_SESSION_STATUSES,
    AiSessionStatus,
    LedgerBucket,
    SessionEndReason,
)
from app.models.ledger import LedgerEntry
from app.models.tts_job import TtsBatchJob
from app.models.wallet import Wallet
from app.services.billing import session_service, wallet_repo

logger = logging.getLogger("synora.billing")

# How long a session has to have been *quiet* before the reaper will touch it,
# on top of being past `expires_at`. Progress is stamped by
# `session_service.touch_session` as bytes move.
#
# Fifteen minutes, and deliberately generous. The floor is the longest silence
# a healthy stream can have: an upstream read is allowed 300 seconds by
# `tts_read_timeout_seconds`, so a stream waiting on a slow speech box is
# legitimately silent for five minutes at a stretch, and a client that pauses a
# download is silent for as long as it likes. The two mistakes are not
# symmetric. Waiting too long costs a customer's credit a quarter of an hour of
# being frozen; not waiting long enough kills a live stream, releases its hold
# and delivers the whole synthesis free with no record it was ever billable.
# When the choice is between late and free, take late.
PROGRESS_GRACE_SECONDS = 900


@dataclass(frozen=True)
class WalletAudit:
    wallet_id: uuid.UUID
    user_id: uuid.UUID
    paid_micros: int
    paid_ledger_micros: int
    bonus_micros: int
    bonus_ledger_micros: int
    reserved_micros: int
    reserved_ledger_micros: int
    reserved_held_micros: int
    entry_count: int
    # The first operation whose recorded `balance_after_*` disagreed with the
    # running total. Names the change that broke it instead of just reporting
    # that something did.
    first_divergent_group_id: uuid.UUID | None
    first_divergent_version: int | None

    @property
    def balances_match(self) -> bool:
        return (
            self.paid_micros == self.paid_ledger_micros
            and self.bonus_micros == self.bonus_ledger_micros
            and self.reserved_micros == self.reserved_ledger_micros
        )

    @property
    def holds_match(self) -> bool:
        return self.reserved_micros == self.reserved_held_micros

    @property
    def is_consistent(self) -> bool:
        return self.balances_match and self.holds_match and self.first_divergent_group_id is None

    @property
    def reserved_drift_micros(self) -> int:
        return self.reserved_micros - self.reserved_held_micros


async def _bucket_totals(session: AsyncSession, wallet_id: uuid.UUID) -> dict[LedgerBucket, int]:
    rows = (
        await session.execute(
            select(LedgerEntry.bucket, func.coalesce(func.sum(LedgerEntry.amount_micros), 0))
            .where(LedgerEntry.wallet_id == wallet_id)
            .group_by(LedgerEntry.bucket)
        )
    ).all()
    return {bucket: total for bucket, total in rows}


async def _first_divergence(
    session: AsyncSession, wallet_id: uuid.UUID
) -> tuple[uuid.UUID | None, int | None, int]:
    """Replay the ledger and return where it first disagreed with itself.

    Grouped by `group_id`, not walked row by row: the rows of one operation
    share a `wallet_version` and record the *same* resulting balance, so a
    per-row running total diverges on the first split debit and reports a
    problem that is not there.
    """
    rows = list(
        (
            await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.wallet_id == wallet_id)
                .order_by(LedgerEntry.wallet_version, LedgerEntry.bucket)
            )
        ).scalars()
    )

    running = {LedgerBucket.PAID: 0, LedgerBucket.BONUS: 0, LedgerBucket.RESERVED: 0}
    group: list[LedgerEntry] = []

    def close(batch: list[LedgerEntry]) -> tuple[uuid.UUID, int] | None:
        for entry in batch:
            running[entry.bucket] += entry.amount_micros
        head = batch[0]
        if (
            running[LedgerBucket.PAID] != head.balance_after_paid_micros
            or running[LedgerBucket.BONUS] != head.balance_after_bonus_micros
            or running[LedgerBucket.RESERVED] != head.balance_after_reserved_micros
        ):
            return head.group_id, head.wallet_version
        return None

    for entry in rows:
        if group and entry.group_id != group[0].group_id:
            found = close(group)
            if found is not None:
                return found[0], found[1], len(rows)
            group = []
        group.append(entry)
    if group:
        found = close(group)
        if found is not None:
            return found[0], found[1], len(rows)

    return None, None, len(rows)


async def verify_wallet(session: AsyncSession, wallet_id: uuid.UUID) -> WalletAudit:
    snapshot = await wallet_repo.snapshot_by_id(session, wallet_id)
    totals = await _bucket_totals(session, wallet_id)

    held = (
        await session.execute(
            select(func.coalesce(func.sum(AiSession.reserved_micros), 0)).where(
                AiSession.wallet_id == wallet_id,
                AiSession.hold_released_at.is_(None),
            )
        )
    ).scalar_one()

    group_id, version, entry_count = await _first_divergence(session, wallet_id)

    return WalletAudit(
        wallet_id=wallet_id,
        user_id=snapshot.user_id,
        paid_micros=snapshot.paid_micros,
        paid_ledger_micros=totals.get(LedgerBucket.PAID, 0),
        bonus_micros=snapshot.bonus_micros,
        bonus_ledger_micros=totals.get(LedgerBucket.BONUS, 0),
        reserved_micros=snapshot.reserved_micros,
        reserved_ledger_micros=totals.get(LedgerBucket.RESERVED, 0),
        reserved_held_micros=held,
        entry_count=entry_count,
        first_divergent_group_id=group_id,
        first_divergent_version=version,
    )


async def heal_reserved(session: AsyncSession, wallet_id: uuid.UUID) -> int:
    """Release credit held by sessions that are over. Returns what it freed.

    Only ever releases, never adds: if `reserved_micros` is somehow *lower*
    than the sessions claim, that is an under-reservation and letting a caller
    spend money they were going to spend anyway is the safer of the two
    mistakes. It is logged and left for a human.

    Sessions in a terminal state with an unreleased hold are the actual bug
    this repairs — they are stamped `hold_released_at` as part of the fix, so
    the same drift is not rediscovered every five minutes.
    """
    audit = await verify_wallet(session, wallet_id)
    drift = audit.reserved_drift_micros
    if drift == 0:
        return 0
    if drift < 0:
        logger.error(
            "reserved_drift wallet=%s under-reserved by %s micros; not healing automatically",
            wallet_id,
            -drift,
        )
        return 0

    logger.error(
        "reserved_drift wallet=%s reserved=%s held_by_sessions=%s releasing=%s",
        wallet_id,
        audit.reserved_micros,
        audit.reserved_held_micros,
        drift,
    )

    now = utcnow()
    await session.execute(
        update(AiSession)
        .where(
            AiSession.wallet_id == wallet_id,
            AiSession.hold_released_at.is_(None),
            AiSession.status.in_(TERMINAL_SESSION_STATUSES),
        )
        .values(hold_released_at=now, reserved_micros=0, updated_at=now)
        .execution_options(synchronize_session=False)
    )

    await wallet_repo.release_hold(
        session,
        wallet_id=wallet_id,
        amount_micros=drift,
        idempotency_key=f"reconcile:{wallet_id}:{audit.reserved_micros}:{audit.reserved_held_micros}",
        note="Released by reconciliation: no live session held it",
    )
    return drift


async def verify_all(session: AsyncSession, *, limit: int = 500) -> list[WalletAudit]:
    """Audit the wallets most likely to have drifted first.

    Ordered by `updated_at` descending because drift is introduced by activity;
    a wallet nobody has touched since the last clean run cannot have broken
    since then.
    """
    wallet_ids = list(
        (
            await session.execute(
                select(Wallet.id).order_by(Wallet.updated_at.desc()).limit(limit)
            )
        ).scalars()
    )
    return [await verify_wallet(session, wallet_id) for wallet_id in wallet_ids]


async def reap_expired_sessions(session: AsyncSession, *, limit: int = 500) -> int:
    """Finish sessions past their deadline that have gone quiet. Returns how many.

    The backstop `AiSession.expires_at` has always promised and nothing has
    ever provided. A metered call places its hold before any work starts, on
    purpose, so that a process dying mid-call leaves evidence rather than free
    work — but evidence is only worth having if something reads it. Every way a
    one-shot can die without settling ends in the same row: `ACTIVE`,
    `reserved_micros` at the full price, `hold_released_at` NULL, and credit
    the customer cannot spend. A client that disconnected before Starlette
    started the response body, so the generator's `finally` never ran; a
    settlement that failed on a dead connection and was logged; a batch upstream
    never accepted, so nothing ever polled it; a worker killed between the hold
    and the charge. Six separate bugs in one review, and one grave.

    Neither of the other passes can see it. `heal_reserved` only stamps
    sessions that are *already* terminal, and `verify_wallet` counts a live
    session's hold as legitimately held — which it normally is — so the drift
    reads zero and the alarm stays quiet while the money stays frozen. The
    deadline is the only column that can separate a call still running from one
    that is never coming back, and reading it is this function's whole job.

    It reuses `session_service` rather than writing the columns itself. That is
    not tidiness: `wallet_repo` is the only module allowed to move a balance,
    `tests/test_billing_invariants.py` walks the source tree to enforce it, and
    a repair job hand-rolling a release is exactly the regression that guard
    exists to catch. It also means a reaped session is stamped by the same code
    every other terminal session is stamped by, so there is one definition of
    "over" instead of two.

    *Which* of those functions, though, is the whole question, and for a long
    time the answer was always `abandon_oneshot` — which made this pass a way
    of getting synthesis for free. The two cases are genuinely different work:

    - A session **nobody ever claimed** is a ticket that was minted and never
      taken up. Nothing was done for it, so it is abandoned at zero and stamped
      `EXPIRED` — the member the enum reserves for exactly that, and the
      difference between "the call died" and "the call never began" for whoever
      reads this table next.
    - A session that **was claimed** is work that was committed. A one-shot's
      hold is placed only after the entire text has been handed to the
      supplier, so a claimed session still holding credit means the synthesis
      was bought whether or not anybody ever came back to report it. Abandoning
      it released the hold and stamped the row `FAILED`, and a client that had
      stalled on purpose then drained the response body into a session already
      terminal: `settle_oneshot` found it terminal, rebuilt a zero, and a
      thousand characters went out unpaid — reproducibly, wallet untouched. So
      it goes to `session_service.settle_at_estimate` instead, which charges
      the price the session was opened at and flags the row `disputed`. A
      charge raised on a deadline rather than on a report can be argued with,
      and the ledger answers that argument with a refund; a charge of zero
      cannot be argued with at all, because there is nothing to point at.

    Two things are past their deadline and still must not be touched, and both
    were live bugs before they were exclusions.

    **A session that is still making progress.** `expires_at` bounds when a
    call *started*, and a streaming response is paced by whoever is reading it:
    an upstream read is allowed five minutes, a client on a slow connection can
    take longer than that to drain a body, and neither is a call that died. A
    reaper acting on the deadline alone released the hold and stamped the
    session `FAILED` while the audio was still going out; the stream then
    settled into a terminal session, rebuilt a zero, and the whole synthesis
    was free with nothing on the row to say it should not have been. So the
    predicate asks for silence as well: past the deadline *and* no progress for
    a whole grace period, which `session_service.touch_session` is what stamps.
    A session that never reported any progress at all — `last_heartbeat_at`
    null — has been silent since it opened and is reapable on the deadline
    alone, which is the shape every stranded hold actually has.

    **A session a live batch job still points at.** A batch's session and its
    job ran on two clocks that could not agree: the session's deadline is
    measured from the moment it was opened, the job's from the moment upstream
    accepted it, and the second is always later — by the broker hop, by the
    retry backoff, by hours when a refused submit is resubmitted. In that
    window this pass terminated the session of a job that was perfectly
    healthy; the job kept polling, upstream rendered the whole corpus, and the
    settlement found the session already terminal and stamped the job succeeded
    with nothing charged. The batch lifecycle owns its own deadline, and owns it
    alone: only `tts_batch_service.expire_job` may terminate a batch's session,
    and this exclusion is what makes that sentence true rather than intended.
    """
    now = utcnow()
    quiet_before = now - timedelta(seconds=PROGRESS_GRACE_SECONDS)
    # `NOT EXISTS` rather than an outer join, so a session with no job at all —
    # every one-shot ever opened — is not paying for a join it does not need.
    live_batch_job = (
        select(TtsBatchJob.id)
        .where(
            TtsBatchJob.ai_session_id == AiSession.id,
            TtsBatchJob.state.not_in(TERMINAL_BATCH_STATES),
        )
        .exists()
    )
    rows = (
        await session.execute(
            select(
                AiSession.id,
                AiSession.claimed_at,
                AiSession.expires_at,
                AiSession.last_heartbeat_at,
                # Only so the log line below can say how much billable work was
                # involved. TTS is the only metered service today and
                # characters its only priced metric; a second service needs its
                # own column here rather than a share of this one.
                AiSession.cum_tts_characters,
            )
            .where(
                AiSession.expires_at.is_not(None),
                AiSession.expires_at < now,
                # Stated as "not terminal" rather than "in the live set", so a
                # status added later is reaped by default. Missing a stuck
                # session is the failure this exists to prevent; visiting an
                # already-finished one costs a no-op.
                AiSession.status.not_in(TERMINAL_SESSION_STATUSES),
                or_(
                    AiSession.last_heartbeat_at.is_(None),
                    AiSession.last_heartbeat_at < quiet_before,
                ),
                ~live_batch_job,
            )
            # Oldest deadline first: if `limit` bites, the credit that has been
            # frozen longest comes back first.
            .order_by(AiSession.expires_at)
            .limit(limit)
        )
    ).all()

    reaped = 0
    for (
        ai_session_id,
        claimed_at,
        expires_at,
        last_progress_at,
        reported_chars,
    ) in rows:
        # Re-read the progress mark instead of trusting the scan. The scan is
        # one statement covering up to `limit` rows and each reap below is a
        # transaction of its own, so by the time this row's turn comes the
        # snapshot can be minutes stale — long enough for a stalled stream to
        # have resumed and started delivering audio again. A primary-key read
        # is a cheap price for not billing that stream at zero.
        fresh_progress_at = (
            await session.execute(
                select(AiSession.last_heartbeat_at).where(AiSession.id == ai_session_id)
            )
        ).scalar_one_or_none()
        if fresh_progress_at is not None and as_utc(fresh_progress_at) > (
            utcnow() - timedelta(seconds=PROGRESS_GRACE_SECONDS)
        ):
            # WARNING, and not silence. A candidate that came back to life
            # between the scan and here is a live stream we came one statement
            # away from closing and settling to nothing, and the only reason
            # anyone would ever know is this line. If it appears at all, the
            # deadline is sized wrong for the work it is bounding.
            logger.warning(
                "session_reap_skipped_progress session=%s expired_at=%s last_progress=%s",
                ai_session_id,
                expires_at,
                fresh_progress_at,
            )
            continue

        # `claimed_at`, not the status, is what splits the two cases. A row
        # can be left `ACTIVE` by a crash between the claim and the first
        # write, and the question being asked is whether work was ever started
        # for it — which only the claim timestamp answers.
        never_claimed = claimed_at is None
        settled_micros = 0
        try:
            if never_claimed:
                # Nothing was done, so nothing is owed.
                await session_service.abandon_oneshot(
                    session,
                    ai_session_id=ai_session_id,
                    status=AiSessionStatus.EXPIRED,
                    end_reason=SessionEndReason.MAX_DURATION,
                    error_code="session_reaped",
                )
            else:
                # Work was committed and no report is coming. Bill it at the
                # price the hold was placed for; `settle_at_estimate` falls
                # back to abandoning the session when that price is zero, so
                # this branch is total.
                settlement = await session_service.settle_at_estimate(
                    session,
                    ai_session_id=ai_session_id,
                    end_reason=SessionEndReason.TIMEOUT,
                    error_code="session_reaped",
                )
                settled_micros = settlement.price_micros
        except Exception:  # noqa: BLE001 - one bad row must not strand the rest
            # A wallet under contention answers `wallet_busy`, and this pass
            # may be holding four hundred and ninety-nine other frozen holds
            # behind it. Roll back whatever the failed attempt left pending so
            # the next iteration starts clean, and let the next run retry this
            # one — the deadline has not moved.
            logger.exception("session_reap_failed session=%s", ai_session_id)
            await session.rollback()
            continue

        reaped += 1
        # WARNING, not INFO, and on every reap. A hold that had to be reaped is
        # evidence of a bug upstream of here — something failed to settle a call
        # it started — and the session id is the thread to pull. Routine
        # housekeeping does not find anything; a quiet reaper is the only
        # healthy reaper.
        logger.warning(
            "session_reaped session=%s claimed=%s expired_at=%s last_progress=%s "
            "settled_micros=%s reported_chars=%s",
            ai_session_id,
            "no" if never_claimed else "yes",
            expires_at,
            # "never" is the ordinary answer and a timestamp is the interesting
            # one: it says the call was alive, delivering, and then stopped
            # without settling — which narrows the bug to whatever runs between
            # the last chunk and `settle_oneshot`.
            last_progress_at or "never",
            # The two halves of "how much billable work was involved", and they
            # normally disagree on purpose. `settled_micros` is what the wallet
            # was actually charged; `reported_chars` is what the session managed
            # to record, which is zero for exactly the sessions this pass exists
            # for — nothing ever reported, which is why the charge had to fall
            # back to the estimate. A reap that bills nothing and recorded
            # nothing is a ticket that never started; a reap that bills the
            # estimate against zero characters is a stream that died mid-flight.
            settled_micros,
            reported_chars,
        )

    return reaped


async def reconcile_all(session: AsyncSession, *, limit: int = 500) -> dict[str, int]:
    """The scheduled pass. Reaps, heals held credit, reports divergence.

    The order is load-bearing. Reaping runs first because it is the pass that
    *creates* work for the other two: it stamps a stuck session terminal and
    gives its hold back, and `heal_reserved`'s repair deliberately refuses to
    touch a row that still looks live. Audit before reap and every hold the
    reaper frees is read as healthy on the way past, then rediscovered as drift
    on the following run — an hour of frozen credit for no reason.
    """
    reaped = await reap_expired_sessions(session, limit=limit)
    audits = await verify_all(session, limit=limit)
    healed = 0
    diverged = 0

    for audit in audits:
        if not audit.holds_match:
            healed += await heal_reserved(session, audit.wallet_id)
        if not audit.balances_match or audit.first_divergent_group_id is not None:
            diverged += 1
            logger.error(
                "ledger_divergence wallet=%s paid=%s/%s bonus=%s/%s reserved=%s/%s "
                "first_bad_group=%s version=%s",
                audit.wallet_id,
                audit.paid_micros,
                audit.paid_ledger_micros,
                audit.bonus_micros,
                audit.bonus_ledger_micros,
                audit.reserved_micros,
                audit.reserved_ledger_micros,
                audit.first_divergent_group_id,
                audit.first_divergent_version,
            )

    if healed or diverged:
        await session.commit()
    # `reaped` carries no micros of its own, and it is not a synonym for
    # freed credit: each reaped session was committed by `session_service`
    # before this returned, but a claimed one was *settled* rather than
    # abandoned, so part of its hold became a charge instead of coming back.
    # The count is what is worth alerting on either way; the amounts are on the
    # `session_reaped` lines.
    return {
        "checked": len(audits),
        "reaped": reaped,
        "healed_micros": healed,
        "diverged": diverged,
    }
