"""ai_sessions: an idempotency key that fits, and a record of what it bought

Two columns, one revision, because they are the same defect seen from two
sides: the key column could not hold the keys we publish, and nothing on the
row said what a stored key had been spent on.

`idempotency_key` was VARCHAR(128) while the value written into it is
`{user_id}:{scope}:{client key}` — thirty-seven characters of uuid and colons
over a client key the OpenAPI schema advertises as up to 128 long, so 172
characters into a column declared 128. Postgres answers that with 22001 and an
unhandled `DataError`, which is a 500 on `POST /tts/speech` for a key our own
documentation calls legal. SQLite ignores a declared VARCHAR width entirely and
stores the whole string, which is why a green test suite proved nothing about
this and never could. Widening to 200 is the direction that keeps the published
contract; narrowing the contract to fit the column would break exactly the
clients that read the docs and believed them.

`request_digest` is new and nullable. A replayed key used to be validated
against the *price* of the session it named, and the price book charges per
thousand characters with CEIL rounding, so every text from one to a thousand
characters priced identically: one paid character bought unlimited free
synthesis inside that bucket. A price is a bucket, and a bucket is not an
identity. Sixty-four characters because the digest is a sha256 hex string.
Existing rows stay null, which the replay guard reads as "cannot vouch for this
replay" and refuses — the safe direction.

Both changes are safe to run before the new code ships and safe to leave in
place if it is rolled back. Widening a VARCHAR on Postgres is a catalogue
change, not a table rewrite, so it takes no lock worth planning a window
around; adding a nullable column with no default is the same. The old code
neither writes longer keys nor reads the digest.

`op.batch_alter_table` rather than a bare `op.alter_column`: SQLite cannot
ALTER a column's type at all and has to have the table rebuilt around the
change, which is what batch mode does. On Postgres batch mode emits the plain
`ALTER TABLE` statements, so one spelling serves both and the migration history
does not have to branch on the dialect.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ai_sessions") as batch:
        batch.alter_column(
            "idempotency_key",
            existing_type=sa.String(length=128),
            type_=sa.String(length=200),
            existing_nullable=True,
        )
        batch.add_column(sa.Column("request_digest", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ai_sessions") as batch:
        batch.drop_column("request_digest")
        # Narrowing back can fail on Postgres with 22001 if any stored key is
        # longer than 128 — which, after this revision has been live for any
        # length of time, is the normal case rather than the exotic one. That
        # is left as a hard error on purpose: silently truncating an
        # idempotency key would make two distinct requests share one key and
        # hand a caller somebody else's paid session. A rollback that needs to
        # get past this has to decide what to do with those rows first.
        batch.alter_column(
            "idempotency_key",
            existing_type=sa.String(length=200),
            type_=sa.String(length=128),
            existing_nullable=True,
        )
