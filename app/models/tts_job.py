"""Batch text-to-speech jobs: the durable half of an asynchronous synthesis.

A batch is a one-shot metered session that outlives the request that created
it. `POST /tts/batch` prices the whole payload, places the hold, writes this
row and hands the work to RabbitMQ; a worker submits it upstream, polls until
upstream reaches a terminal state, then settles the session against what
upstream says it actually produced. Between those two moments this row is the
only thing that remembers the job exists.

Which is why the items are stored here rather than only in the queue message.
Between "we took the hold" and "upstream acknowledged the job" the text exists
in exactly one place, and if that place is a broker message then one lost
delivery — a broker restart, a worker killed mid-ack, a queue purged by
someone tidying up — leaves us holding the user's credit with no
`upstream_job_id` to poll and no text to resubmit. That is unrecoverable work
the user has already paid a hold for, and no amount of retrying finds it
again. Keeping `items_json` on the row turns a redelivery, a worker crash and
a broker outage into the same recoverable thing: read the payload back, submit
it again under the same idempotency key, get the same upstream job. It is
cleared the moment `upstream_job_id` is set, because from then on upstream
holds the copy that matters and ours is only weight in the row.

`audio_ms` is recorded and never priced, and that is a decision rather than an
oversight. TTS is sold by input characters — the one quantity a caller can
count before spending anything — so the price book has a `tts`/`tts_characters`
row and no `tts`/`tts_audio_ms` row at all. `pricing.price_cumulative` raises
`BadRequestError` for any metric with a quantity above zero and no price, so
reporting seconds of audio into `AiSession.cum_tts_audio_ms` would turn every
batch settlement into a 400 — the charge would fail, not the metric. The number
is still worth keeping, because it is how the GPU-side cost of a job is
measured, so it lives here on the job row where it can be observed without ever
touching the priced path.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.billing_enums import TERMINAL_BATCH_STATES, TtsBatchJobState


class TtsBatchJob(Base):
    __tablename__ = "tts_batch_jobs"
    __table_args__ = (
        # Upstream's handle, once it has issued one. Unique so that a
        # redelivered submit which raced our own commit cannot end up with two
        # rows polling one upstream job and settling it twice.
        UniqueConstraint("upstream_job_id", name="uq_tts_batch_jobs_upstream_job_id"),
        # Stored as `"{user_id}:{key}"`. Scoping the key to the user is what
        # stops one account's replay from returning another account's job when
        # a shared client library ships a fixed default key.
        UniqueConstraint("idempotency_key", name="uq_tts_batch_jobs_idempotency_key"),
        # One job, one metered session. Settlement reaches the session through
        # this column, so two rows pointing at one session would be two
        # settlements of one hold.
        UniqueConstraint("ai_session_id", name="uq_tts_batch_jobs_ai_session_id"),
        CheckConstraint("total_items >= 0", name="total_items_nonneg"),
        CheckConstraint("completed_items >= 0", name="completed_items_nonneg"),
        CheckConstraint("failed_items >= 0", name="failed_items_nonneg"),
        CheckConstraint("submitted_characters >= 0", name="submitted_characters_nonneg"),
        CheckConstraint("billed_characters >= 0", name="billed_characters_nonneg"),
        CheckConstraint("audio_ms >= 0", name="audio_ms_nonneg"),
        CheckConstraint("poll_count >= 0", name="poll_count_nonneg"),
        Index("ix_tts_batch_jobs_user_created", "user_id", "created_at"),
        # The poller's sweep: what is due, and when. `state` leads because it
        # is the selective half — a terminal job is never a candidate, and by
        # then `next_poll_at` is null anyway.
        Index("ix_tts_batch_jobs_state_next_poll", "state", "next_poll_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )  # covered by ix_tts_batch_jobs_user_created
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    # The hold, the price book pin and the eventual debit all live on the
    # session; this row only ever carries the upstream half of the story.
    ai_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="RESTRICT"), nullable=False
    )  # covered by uq_tts_batch_jobs_ai_session_id

    state: Mapped[TtsBatchJobState] = mapped_column(
        Enum(TtsBatchJobState, native_enum=False, length=32),
        default=TtsBatchJobState.QUEUED,
        server_default=TtsBatchJobState.QUEUED.value,
        nullable=False,
    )
    upstream_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Longer than the 128 the other tables use because the user's own key is
    # prefixed with a 36-character UUID and a colon before it is stored.
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # --- synthesis parameters ----------------------------------------------
    # Pinned at creation and replayed verbatim on every resubmit, so a retry
    # cannot quietly render in a different voice or format than the one the
    # user asked for and we quoted.
    voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_format: Mapped[str] = mapped_column(String(16), nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    style: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- progress, as upstream reports it ----------------------------------
    total_items: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    completed_items: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    failed_items: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # --- quantities --------------------------------------------------------
    # What we counted out of the payload ourselves and placed the hold against.
    submitted_characters: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # What upstream reported and what the settlement actually charged. Lands
    # below `submitted_characters` whenever items failed: upstream counts what
    # it synthesised, and nobody is billed for audio that was never produced.
    billed_characters: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # Observability only — see the module docstring. This number must never
    # reach `AiSession.cum_tts_audio_ms` or `pricing.price_cumulative`.
    audio_ms: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    # --- the payload, until upstream owns it -------------------------------
    # The `items` array of the upstream request, JSON-serialised, and null
    # again as soon as `upstream_job_id` is set. `Text` rather than a bounded
    # `String`: the only other blob in this schema, `Payment.raw_payload`, is
    # capped at 16 KB because provider callbacks are small, but
    # `tts_batch_max_characters` alone puts half a megabyte of text in here on
    # a large job, and truncating this one would destroy the very thing it
    # exists to preserve.
    items_json: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # --- polling and outcome -----------------------------------------------
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # How many times we have asked upstream. A job that is merely slow and a
    # job that is stuck look identical without it.
    poll_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the next poll is due. Null whenever there is nothing to poll —
    # before submission, and after a terminal state — which keeps a settled
    # job out of the sweep even if its row is never touched again.
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_terminal(self) -> bool:
        """True once the hold is gone: nothing further is polled or charged."""
        return self.state in TERMINAL_BATCH_STATES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TtsBatchJob {self.state.value} {self.completed_items}/{self.total_items}>"
