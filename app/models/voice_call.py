"""One voice-agent call: the signalling we relayed and the heartbeats we saw.

The other metered sessions all have a socket or a request on our side of the
gateway, so whatever ends them ends here too and the settlement knows exactly
how long they ran. A voice call does not. The browser and the agent exchange
audio directly over WebRTC; what passes through us is the SDP offer and answer
and a handful of ICE candidates, all within the first second. After that the
only thing that tells us a call is still going is the client saying so, which
is what `last_seen_at` records.

## An extension of `ai_sessions`, not a second session

`id` *is* the `ai_sessions.id` of the metered session behind the call — a
primary key that is also the foreign key. One id, then, on the URL, in the
ledger and in `GET /usage`, and every money column stays where every other
service keeps it: status, hold, settlement, end reason and the billed
`cum_session_ms` all live on the `AiSession` row. Nothing here is on the money
path except the three timestamps that decide how long the call is billed for.

## The three timestamps billing reads

- `answered_at` — the agent accepted the offer and built a pipeline for the
  call. The ceiling runs from here, and so did the hold's price.
- `connected_at` — the first heartbeat, which a client sends once its media is
  up, and where billable time starts, on our clock, never the client's. A call
  that never reaches it never connected: ICE failed, or the tab went away
  before it could, and it is released at zero.
- `last_seen_at` — the latest proof of life. Non-null from the moment the row
  exists, so the sweeper's query is a plain range scan on a partial index
  rather than a `COALESCE` no index can serve.

`ended_at` is non-null exactly once, when the call was settled, and is what
"live" means to every query on this table.

## What the agent can vouch for

The agent will not tell us a call ended, but it will answer whether it still
holds one: `PATCH /api/offer` with an empty candidate list is a no-op on a peer
connection it holds and a 404 on one it has dropped. `agent_seen_at` is the
last time it said "still here" about a call whose client had gone quiet — the
evidence that a call kept running after its heartbeats stopped, which is billed
rather than given away. `gone_at` is when it said "not mine any more", which is
what finally lets a call stop counting against its user's line.

"Still holds it" is not "connected", though. A peer the browser closed before
sending a single ICE candidate sits in ICE checking with nothing to check, and
the live agent keeps such a peer for about a minute before dropping it — an
aiortc agent without that timeout keeps it forever. `nudged_at` is when we
relayed an unroutable candidate to a call that never heartbeated: a peer stuck
with no pairs then gets one that fails in about a minute, so one still held
well after the nudge is one that really did connect.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The scope `session_service.open_oneshot` mints a voice call's session key
# under — `{user_id}:voice:auto:{hex}`, since `open_call` never passes a key of
# its own. Here rather than in the service because the reaper in
# `reconcile_service` has to recognise these sessions too, and billing code may
# import a model but never an AI service.
IDEMPOTENCY_SCOPE = "voice"
OWN_SESSION_KEY_PATTERN = f"%:{IDEMPOTENCY_SCOPE}:auto:%"

# Pipecat-style ids are short — a class name and a counter — but this is the
# agent's value, not ours, and a longer one is refused as unreadable rather
# than truncated into a handle that names a different call.
UPSTREAM_PC_ID_MAX_LENGTH = 128


class VoiceCall(Base):
    """A call's signalling handle and liveness. `created_at`/`updated_at` from `Base`."""

    __tablename__ = "voice_calls"
    __table_args__ = (
        CheckConstraint("heartbeats >= 0", name="heartbeats_nonneg"),
        CheckConstraint("candidates >= 0", name="candidates_nonneg"),
        Index("ix_voice_calls_user_created", "user_id", "created_at", "id"),
        # "How many calls does this user have up right now", on every open.
        # Partial, because an ended call is the overwhelming majority of rows
        # and never the answer.
        Index(
            "ix_voice_calls_live_user",
            "user_id",
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
        # The sweeper's query: live calls ordered by how long they have been
        # quiet.
        Index(
            "ix_voice_calls_live_seen",
            "last_seen_at",
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
    )

    # Redeclared from `Base`: the key is the metered session's own id, set by
    # hand at insert, so it has no default of its own to fall back on.
    id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # The agent's handle for the peer connection, from its answer. Needed to
    # relay candidates, and never shown to the browser: it is the supplier's
    # identifier, and our session id already names the call.
    upstream_pc_id: Mapped[str | None] = mapped_column(
        String(UPSTREAM_PC_ID_MAX_LENGTH), nullable=True
    )

    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nudged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    heartbeats: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # ICE candidates relayed, capped per call: each one is an outbound request
    # to the agent, and a client that loops on `onicecandidate` should run into
    # a refusal rather than into the agent's rate limit on our shared key.
    candidates: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "ended" if self.ended_at else "live"
        return f"<VoiceCall {self.id} {state}>"
