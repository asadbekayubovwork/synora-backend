"""billing core: wallets, ledger, price books, sessions, usage, top-ups

Adds the whole metering and prepaid-credit schema, plus `users.is_superuser`
for the admin routes.

Everything here is additive, so the deploy can migrate before restarting.

`server_default` uses `sa.func.now()` rather than a literal, so this renders
`now()` on Postgres and `CURRENT_TIMESTAMP` on SQLite instead of baking one
dialect's spelling into the history.

The append-only guard on `ledger_entries` is a separate revision (0003),
because it is a Postgres trigger and has nothing to do with table shape.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def upgrade() -> None:
    op.create_table('credit_rates',
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('uzs_per_credit_tiyin', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'ACTIVE', 'RETIRED', name='creditratestatus', native_enum=False, length=32), server_default='draft', nullable=False),
    sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name=op.f('ck_credit_rates_window_ordered')),
    sa.CheckConstraint('uzs_per_credit_tiyin > 0', name=op.f('ck_credit_rates_rate_positive')),
    sa.CheckConstraint('version > 0', name=op.f('ck_credit_rates_version_positive')),
    sa.ForeignKeyConstraint(['published_by_user_id'], ['users.id'], name=op.f('fk_credit_rates_published_by_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_rates')),
    sa.UniqueConstraint('version', name='uq_credit_rates_version')
    )
    op.create_index('ix_credit_rates_status_effective', 'credit_rates', ['status', 'effective_from'], unique=False)
    op.create_table('price_book_versions',
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'ACTIVE', 'RETIRED', name='pricebookstatus', native_enum=False, length=32), server_default='draft', nullable=False),
    sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('notes', sa.String(length=1024), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name=op.f('ck_price_book_versions_window_ordered')),
    sa.CheckConstraint('version > 0', name=op.f('ck_price_book_versions_version_positive')),
    sa.ForeignKeyConstraint(['published_by_user_id'], ['users.id'], name=op.f('fk_price_book_versions_published_by_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_price_book_versions')),
    sa.UniqueConstraint('version', name='uq_price_book_versions_version')
    )
    op.create_index('ix_price_book_versions_status_effective', 'price_book_versions', ['status', 'effective_from'], unique=False)
    op.create_table('service_api_keys',
    sa.Column('label', sa.String(length=255), nullable=False),
    sa.Column('service', sa.Enum('TTS', 'STT', 'CHAT', 'VOICE_AGENT', name='billingservice', native_enum=False, length=32), nullable=True),
    sa.Column('key_id', sa.String(length=64), nullable=False),
    sa.Column('key_version', sa.BigInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('scopes', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_ip', sa.String(length=64), nullable=True),
    sa.Column('use_count', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('key_version > 0', name=op.f('ck_service_api_keys_key_version_positive')),
    sa.CheckConstraint('use_count >= 0', name=op.f('ck_service_api_keys_use_count_nonneg')),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_service_api_keys_created_by_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_service_api_keys')),
    sa.UniqueConstraint('key_id', name='uq_service_api_keys_key_id')
    )
    op.create_index('ix_service_api_keys_is_active', 'service_api_keys', ['is_active'], unique=False)
    op.create_table('usage_daily_rollups',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('service', sa.Enum('TTS', 'STT', 'CHAT', 'VOICE_AGENT', name='billingservice', native_enum=False, length=32), nullable=False),
    sa.Column('model_key', sa.String(length=128), nullable=False),
    sa.Column('metric', sa.Enum('SESSION_MS', 'STT_AUDIO_MS', 'TTS_CHARACTERS', 'TTS_AUDIO_MS', 'LLM_INPUT_TOKENS', 'LLM_CACHED_INPUT_TOKENS', 'LLM_OUTPUT_TOKENS', name='usagemetric', native_enum=False, length=32), nullable=False),
    sa.Column('quantity', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('price_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cost_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('writeoff_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('event_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_event_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('event_count >= 0', name=op.f('ck_usage_daily_rollups_event_count_nonneg')),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_usage_daily_rollups_quantity_nonneg')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_usage_daily_rollups_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_daily_rollups')),
    sa.UniqueConstraint('user_id', 'day', 'service', 'model_key', 'metric', name='uq_usage_daily_rollups_dimension')
    )
    op.create_index('ix_usage_daily_rollups_day', 'usage_daily_rollups', ['day'], unique=False)
    op.create_index('ix_usage_daily_rollups_user_day', 'usage_daily_rollups', ['user_id', 'day'], unique=False)
    op.create_table('wallets',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('paid_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('bonus_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('bonus_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reserved_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('version', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('frozen_reason', sa.String(length=255), nullable=True),
    sa.Column('low_balance_threshold_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('low_balance_notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lifetime_spend_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('lifetime_topup_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('lifetime_writeoff_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('bonus_micros >= 0', name=op.f('ck_wallets_bonus_nonneg')),
    sa.CheckConstraint('lifetime_spend_micros >= 0', name=op.f('ck_wallets_lifetime_spend_nonneg')),
    sa.CheckConstraint('lifetime_topup_micros >= 0', name=op.f('ck_wallets_lifetime_topup_nonneg')),
    sa.CheckConstraint('lifetime_writeoff_micros >= 0', name=op.f('ck_wallets_lifetime_writeoff_nonneg')),
    sa.CheckConstraint('low_balance_threshold_micros >= 0', name=op.f('ck_wallets_low_balance_threshold_nonneg')),
    sa.CheckConstraint('paid_micros >= 0', name=op.f('ck_wallets_paid_nonneg')),
    sa.CheckConstraint('reserved_micros >= 0', name=op.f('ck_wallets_reserved_nonneg')),
    sa.CheckConstraint('version >= 0', name=op.f('ck_wallets_version_nonneg')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_wallets_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wallets')),
    sa.UniqueConstraint('user_id', name='uq_wallets_user')
    )
    op.create_index(op.f('ix_wallets_user_id'), 'wallets', ['user_id'], unique=False)
    op.create_table('ai_sessions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('wallet_id', sa.Uuid(), nullable=False),
    sa.Column('service', sa.Enum('TTS', 'STT', 'CHAT', 'VOICE_AGENT', name='billingservice', native_enum=False, length=32), nullable=False),
    sa.Column('kind', sa.Enum('ONESHOT', 'REALTIME', name='aisessionkind', native_enum=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'ACTIVE', 'GRACE', 'CLOSING', 'CLOSED', 'EXPIRED', 'KILLED', 'FAILED', name='aisessionstatus', native_enum=False, length=32), server_default='pending', nullable=False),
    sa.Column('model_key', sa.String(length=128), nullable=False),
    sa.Column('price_book_version_id', sa.Uuid(), nullable=False),
    sa.Column('reserved_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('hold_peak_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('settled_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('estimated_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('writeoff_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cost_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cum_session_ms', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cum_stt_audio_ms', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cum_tts_characters', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cum_tts_audio_ms', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cum_llm_input_tokens', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cum_llm_cached_input_tokens', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cum_llm_output_tokens', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('hold_released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_sequence', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('resume_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('grace_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('grace_micros_granted', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('end_reason', sa.Enum('COMPLETED', 'CLIENT_HANGUP', 'CLIENT_DISCONNECTED', 'STOP_REQUESTED', 'USER_CANCELLED', 'ADMIN_KILLED', 'INSUFFICIENT_CREDIT', 'GRACE_EXHAUSTED', 'HEARTBEAT_TIMEOUT', 'MAX_DURATION', 'NEVER_CLAIMED', 'BACKEND_UNREACHABLE', 'UPSTREAM_ERROR', 'INTERNAL_ERROR', 'TIMEOUT', name='sessionendreason', native_enum=False, length=32), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('disputed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('authorize_jti', sa.String(length=64), nullable=False),
    sa.Column('service_api_key_id', sa.Uuid(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('client_ip', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('version', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('cost_micros >= 0', name=op.f('ck_ai_sessions_cost_nonneg')),
    sa.CheckConstraint('estimated_micros >= 0', name=op.f('ck_ai_sessions_estimated_nonneg')),
    sa.CheckConstraint('last_sequence >= 0', name=op.f('ck_ai_sessions_last_sequence_nonneg')),
    sa.CheckConstraint('reserved_micros >= 0', name=op.f('ck_ai_sessions_reserved_nonneg')),
    sa.CheckConstraint('settled_micros >= 0', name=op.f('ck_ai_sessions_settled_nonneg')),
    sa.CheckConstraint('version >= 0', name=op.f('ck_ai_sessions_version_nonneg')),
    sa.CheckConstraint('writeoff_micros >= 0', name=op.f('ck_ai_sessions_writeoff_nonneg')),
    sa.ForeignKeyConstraint(['price_book_version_id'], ['price_book_versions.id'], name=op.f('fk_ai_sessions_price_book_version_id_price_book_versions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['service_api_key_id'], ['service_api_keys.id'], name=op.f('fk_ai_sessions_service_api_key_id_service_api_keys'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ai_sessions_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], name=op.f('fk_ai_sessions_wallet_id_wallets'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_sessions')),
    sa.UniqueConstraint('authorize_jti', name='uq_ai_sessions_authorize_jti'),
    sa.UniqueConstraint('idempotency_key', name='uq_ai_sessions_idempotency_key')
    )
    op.create_index(op.f('ix_ai_sessions_service_api_key_id'), 'ai_sessions', ['service_api_key_id'], unique=False)
    op.create_index('ix_ai_sessions_status_heartbeat', 'ai_sessions', ['status', 'last_heartbeat_at'], unique=False)
    op.create_index('ix_ai_sessions_unreleased_holds', 'ai_sessions', ['wallet_id'], unique=False, postgresql_where=sa.text('hold_released_at IS NULL'), sqlite_where=sa.text('hold_released_at IS NULL'))
    op.create_index('ix_ai_sessions_user_created', 'ai_sessions', ['user_id', 'created_at'], unique=False)
    op.create_index('ix_ai_sessions_wallet_status', 'ai_sessions', ['wallet_id', 'status'], unique=False)
    op.create_table('prices',
    sa.Column('price_book_version_id', sa.Uuid(), nullable=False),
    sa.Column('service', sa.Enum('TTS', 'STT', 'CHAT', 'VOICE_AGENT', name='billingservice', native_enum=False, length=32), nullable=False),
    sa.Column('model_key', sa.String(length=128), server_default='*', nullable=False),
    sa.Column('metric', sa.Enum('SESSION_MS', 'STT_AUDIO_MS', 'TTS_CHARACTERS', 'TTS_AUDIO_MS', 'LLM_INPUT_TOKENS', 'LLM_CACHED_INPUT_TOKENS', 'LLM_OUTPUT_TOKENS', name='usagemetric', native_enum=False, length=32), nullable=False),
    sa.Column('unit_size', sa.BigInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('price_micros_per_unit', sa.BigInteger(), nullable=False),
    sa.Column('rounding', sa.Enum('CEIL', 'FLOOR', 'HALF_UP', 'EXACT', name='roundingmode', native_enum=False, length=32), server_default='ceil', nullable=False),
    sa.Column('min_charge_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('included_quantity', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cost_micros_per_unit', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('display_unit', sa.String(length=64), nullable=True),
    sa.Column('notes', sa.String(length=255), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('cost_micros_per_unit >= 0', name=op.f('ck_prices_cost_nonneg')),
    sa.CheckConstraint('included_quantity >= 0', name=op.f('ck_prices_included_nonneg')),
    sa.CheckConstraint('min_charge_micros >= 0', name=op.f('ck_prices_min_charge_nonneg')),
    sa.CheckConstraint('price_micros_per_unit >= 0', name=op.f('ck_prices_rate_nonneg')),
    sa.CheckConstraint('unit_size > 0', name=op.f('ck_prices_unit_size_positive')),
    sa.ForeignKeyConstraint(['price_book_version_id'], ['price_book_versions.id'], name=op.f('fk_prices_price_book_version_id_price_book_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_prices')),
    sa.UniqueConstraint('price_book_version_id', 'service', 'model_key', 'metric', name='uq_prices_dimension')
    )
    op.create_index('ix_prices_lookup', 'prices', ['price_book_version_id', 'service', 'metric'], unique=False)
    op.create_table('topups',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('wallet_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.Enum('PAYME', 'CLICK', 'MANUAL', 'PROMO', name='topupprovider', native_enum=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('CREATED', 'PREPARED', 'PAID', 'CREDITED', 'CANCELLED', 'FAILED', 'REFUNDED', 'EXPIRED', name='topupstatus', native_enum=False, length=32), server_default='created', nullable=False),
    sa.Column('order_key', sa.String(length=64), nullable=False),
    sa.Column('amount_tiyin', sa.BigInteger(), nullable=False),
    sa.Column('uzs_per_credit_tiyin', sa.BigInteger(), nullable=False),
    sa.Column('credit_rate_id', sa.Uuid(), nullable=True),
    sa.Column('credit_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('bonus_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('bonus_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('promo_code', sa.String(length=64), nullable=True),
    sa.Column('refunded_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('ledger_group_id', sa.Uuid(), nullable=True),
    sa.Column('credited_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('prepared_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_refreshed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('prepare_id', sa.Integer(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('created_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('version', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('amount_tiyin >= 0', name=op.f('ck_topups_amount_nonneg')),
    sa.CheckConstraint('bonus_micros >= 0', name=op.f('ck_topups_bonus_nonneg')),
    sa.CheckConstraint('credit_micros >= 0', name=op.f('ck_topups_credit_nonneg')),
    sa.CheckConstraint('refunded_micros <= credit_micros + bonus_micros', name=op.f('ck_topups_refund_within_grant')),
    sa.CheckConstraint('refunded_micros >= 0', name=op.f('ck_topups_refunded_nonneg')),
    sa.CheckConstraint('uzs_per_credit_tiyin > 0', name=op.f('ck_topups_rate_positive')),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_topups_created_by_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['credit_rate_id'], ['credit_rates.id'], name=op.f('fk_topups_credit_rate_id_credit_rates'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_topups_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], name=op.f('fk_topups_wallet_id_wallets'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_topups')),
    sa.UniqueConstraint('idempotency_key', name='uq_topups_idempotency_key'),
    sa.UniqueConstraint('order_key', name='uq_topups_order_key')
    )
    op.create_index('ix_topups_status_created', 'topups', ['status', 'created_at'], unique=False)
    op.create_index('ix_topups_user_created', 'topups', ['user_id', 'created_at'], unique=False)
    op.create_table('payments',
    sa.Column('topup_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.Enum('PAYME', 'CLICK', 'MANUAL', 'PROMO', name='topupprovider', native_enum=False, length=32), nullable=False),
    sa.Column('provider_ref', sa.String(length=128), nullable=False),
    sa.Column('provider_state_code', sa.String(length=32), nullable=True),
    sa.Column('state', sa.Enum('CREATED', 'AUTHORIZED', 'CAPTURED', 'CANCELLED', 'REFUNDED', 'FAILED', name='paymentstate', native_enum=False, length=32), server_default='created', nullable=False),
    sa.Column('amount_tiyin', sa.BigInteger(), nullable=False),
    sa.Column('provider_created_ms', sa.BigInteger(), nullable=True),
    sa.Column('provider_performed_ms', sa.BigInteger(), nullable=True),
    sa.Column('provider_cancelled_ms', sa.BigInteger(), nullable=True),
    sa.Column('authorized_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason_code', sa.String(length=32), nullable=True),
    sa.Column('raw_payload', sa.String(length=16384), nullable=True),
    sa.Column('raw_payload_truncated', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('signature_ok', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('callback_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('amount_tiyin >= 0', name=op.f('ck_payments_amount_nonneg')),
    sa.CheckConstraint('callback_count >= 0', name=op.f('ck_payments_callback_count_nonneg')),
    sa.ForeignKeyConstraint(['topup_id'], ['topups.id'], name=op.f('fk_payments_topup_id_topups'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_payments_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    sa.UniqueConstraint('provider', 'provider_ref', name='uq_payments_provider_provider_ref')
    )
    op.create_index('ix_payments_state', 'payments', ['state'], unique=False)
    op.create_index('ix_payments_topup', 'payments', ['topup_id'], unique=False)
    op.create_index(op.f('ix_payments_user_id'), 'payments', ['user_id'], unique=False)
    op.create_table('usage_events',
    sa.Column('ai_session_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('wallet_id', sa.Uuid(), nullable=False),
    sa.Column('service', sa.Enum('TTS', 'STT', 'CHAT', 'VOICE_AGENT', name='billingservice', native_enum=False, length=32), nullable=False),
    sa.Column('model_key', sa.String(length=128), nullable=False),
    sa.Column('price_book_version_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('ONESHOT', 'HEARTBEAT', 'FINAL', 'CORRECTION', name='usageeventkind', native_enum=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('RECORDED', 'PENDING', 'ABANDONED', 'REJECTED', 'VOIDED', name='usageeventstatus', native_enum=False, length=32), server_default='recorded', nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('price_micros', sa.BigInteger(), nullable=False),
    sa.Column('cost_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cumulative_price_micros', sa.BigInteger(), nullable=False),
    sa.Column('debited_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('writeoff_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('ledger_group_id', sa.Uuid(), nullable=True),
    sa.Column('clamped', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('upstream_request_id', sa.String(length=128), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('cost_micros >= 0', name=op.f('ck_usage_events_cost_nonneg')),
    sa.CheckConstraint('debited_micros + writeoff_micros <= price_micros', name=op.f('ck_usage_events_debit_within_price')),
    sa.CheckConstraint('debited_micros >= 0', name=op.f('ck_usage_events_debited_nonneg')),
    sa.CheckConstraint('price_micros >= 0', name=op.f('ck_usage_events_price_nonneg')),
    sa.CheckConstraint('sequence > 0', name=op.f('ck_usage_events_sequence_positive')),
    sa.CheckConstraint('writeoff_micros >= 0', name=op.f('ck_usage_events_writeoff_nonneg')),
    sa.ForeignKeyConstraint(['ai_session_id'], ['ai_sessions.id'], name=op.f('fk_usage_events_ai_session_id_ai_sessions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['price_book_version_id'], ['price_book_versions.id'], name=op.f('fk_usage_events_price_book_version_id_price_book_versions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_usage_events_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], name=op.f('fk_usage_events_wallet_id_wallets'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_events')),
    sa.UniqueConstraint('ai_session_id', 'sequence', name='uq_usage_events_session_sequence'),
    sa.UniqueConstraint('idempotency_key', name='uq_usage_events_idempotency_key')
    )
    op.create_index('ix_usage_events_status_created', 'usage_events', ['status', 'created_at'], unique=False)
    op.create_index('ix_usage_events_user_occurred', 'usage_events', ['user_id', 'occurred_at'], unique=False)
    op.create_index('ix_usage_events_wallet_created', 'usage_events', ['wallet_id', 'created_at'], unique=False)
    op.create_table('ledger_entries',
    sa.Column('wallet_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('TOPUP', 'BONUS_GRANT', 'DEBIT', 'REFUND', 'HOLD', 'RELEASE', 'ADJUSTMENT', 'EXPIRY', 'REVERSAL', name='ledgerentrykind', native_enum=False, length=32), nullable=False),
    sa.Column('bucket', sa.Enum('PAID', 'BONUS', 'RESERVED', name='ledgerbucket', native_enum=False, length=32), nullable=False),
    sa.Column('amount_micros', sa.BigInteger(), nullable=False),
    sa.Column('balance_after_paid_micros', sa.BigInteger(), nullable=False),
    sa.Column('balance_after_bonus_micros', sa.BigInteger(), nullable=False),
    sa.Column('balance_after_reserved_micros', sa.BigInteger(), nullable=False),
    sa.Column('wallet_version', sa.BigInteger(), nullable=False),
    sa.Column('group_id', sa.Uuid(), nullable=False),
    sa.Column('ref_type', sa.Enum('TOPUP', 'PAYMENT', 'AI_SESSION', 'USAGE_EVENT', 'ADMIN_GRANT', 'SIGNUP_BONUS', 'BONUS_EXPIRY', 'RECONCILE', name='ledgerreftype', native_enum=False, length=32), nullable=False),
    sa.Column('topup_id', sa.Uuid(), nullable=True),
    sa.Column('payment_id', sa.Uuid(), nullable=True),
    sa.Column('ai_session_id', sa.Uuid(), nullable=True),
    sa.Column('usage_event_id', sa.Uuid(), nullable=True),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('(CASE WHEN topup_id IS NULL THEN 0 ELSE 1 END + CASE WHEN payment_id IS NULL THEN 0 ELSE 1 END + CASE WHEN ai_session_id IS NULL THEN 0 ELSE 1 END + CASE WHEN usage_event_id IS NULL THEN 0 ELSE 1 END) <= 1', name=op.f('ck_ledger_entries_single_reference')),
    sa.CheckConstraint('amount_micros <> 0', name=op.f('ck_ledger_entries_amount_nonzero')),
    sa.CheckConstraint('wallet_version > 0', name=op.f('ck_ledger_entries_wallet_version_positive')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_ledger_entries_actor_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ai_session_id'], ['ai_sessions.id'], name=op.f('fk_ledger_entries_ai_session_id_ai_sessions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_ledger_entries_payment_id_payments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['topup_id'], ['topups.id'], name=op.f('fk_ledger_entries_topup_id_topups'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['usage_event_id'], ['usage_events.id'], name=op.f('fk_ledger_entries_usage_event_id_usage_events'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ledger_entries_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], name=op.f('fk_ledger_entries_wallet_id_wallets'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ledger_entries')),
    sa.UniqueConstraint('idempotency_key', 'bucket', name='uq_ledger_entries_idempotency_bucket')
    )
    op.create_index(op.f('ix_ledger_entries_ai_session_id'), 'ledger_entries', ['ai_session_id'], unique=False)
    op.create_index('ix_ledger_entries_group', 'ledger_entries', ['group_id'], unique=False)
    op.create_index(op.f('ix_ledger_entries_payment_id'), 'ledger_entries', ['payment_id'], unique=False)
    op.create_index(op.f('ix_ledger_entries_topup_id'), 'ledger_entries', ['topup_id'], unique=False)
    op.create_index(op.f('ix_ledger_entries_usage_event_id'), 'ledger_entries', ['usage_event_id'], unique=False)
    op.create_index(op.f('ix_ledger_entries_user_id'), 'ledger_entries', ['user_id'], unique=False)
    op.create_index('ix_ledger_entries_wallet_created', 'ledger_entries', ['wallet_id', 'created_at'], unique=False)
    op.create_index('ix_ledger_entries_wallet_version', 'ledger_entries', ['wallet_id', 'wallet_version'], unique=False)
    op.create_table('usage_event_items',
    sa.Column('usage_event_id', sa.Uuid(), nullable=False),
    sa.Column('metric', sa.Enum('SESSION_MS', 'STT_AUDIO_MS', 'TTS_CHARACTERS', 'TTS_AUDIO_MS', 'LLM_INPUT_TOKENS', 'LLM_CACHED_INPUT_TOKENS', 'LLM_OUTPUT_TOKENS', name='usagemetric', native_enum=False, length=32), nullable=False),
    sa.Column('quantity', sa.BigInteger(), nullable=False),
    sa.Column('cumulative_quantity', sa.BigInteger(), nullable=False),
    sa.Column('price_id', sa.Uuid(), nullable=False),
    sa.Column('unit_size', sa.BigInteger(), nullable=False),
    sa.Column('price_micros_per_unit', sa.BigInteger(), nullable=False),
    sa.Column('rounding', sa.Enum('CEIL', 'FLOOR', 'HALF_UP', 'EXACT', name='roundingmode', native_enum=False, length=32), nullable=False),
    sa.Column('price_micros', sa.BigInteger(), nullable=False),
    sa.Column('cumulative_price_micros', sa.BigInteger(), nullable=False),
    sa.Column('cost_micros', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    sa.CheckConstraint('cumulative_quantity >= 0', name=op.f('ck_usage_event_items_cumulative_nonneg')),
    sa.CheckConstraint('price_micros >= 0', name=op.f('ck_usage_event_items_price_nonneg')),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_usage_event_items_quantity_nonneg')),
    sa.CheckConstraint('unit_size > 0', name=op.f('ck_usage_event_items_unit_size_positive')),
    sa.ForeignKeyConstraint(['price_id'], ['prices.id'], name=op.f('fk_usage_event_items_price_id_prices'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['usage_event_id'], ['usage_events.id'], name=op.f('fk_usage_event_items_usage_event_id_usage_events'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_event_items')),
    sa.UniqueConstraint('usage_event_id', 'metric', name='uq_usage_event_items_event_metric')
    )
    op.create_index('ix_usage_event_items_metric', 'usage_event_items', ['metric'], unique=False)
    op.create_index(op.f('ix_usage_event_items_price_id'), 'usage_event_items', ['price_id'], unique=False)
    op.add_column('users', sa.Column('is_superuser', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'is_superuser')
    op.drop_index(op.f('ix_usage_event_items_price_id'), table_name='usage_event_items')
    op.drop_index('ix_usage_event_items_metric', table_name='usage_event_items')
    op.drop_table('usage_event_items')
    op.drop_index('ix_ledger_entries_wallet_version', table_name='ledger_entries')
    op.drop_index('ix_ledger_entries_wallet_created', table_name='ledger_entries')
    op.drop_index(op.f('ix_ledger_entries_user_id'), table_name='ledger_entries')
    op.drop_index(op.f('ix_ledger_entries_usage_event_id'), table_name='ledger_entries')
    op.drop_index(op.f('ix_ledger_entries_topup_id'), table_name='ledger_entries')
    op.drop_index(op.f('ix_ledger_entries_payment_id'), table_name='ledger_entries')
    op.drop_index('ix_ledger_entries_group', table_name='ledger_entries')
    op.drop_index(op.f('ix_ledger_entries_ai_session_id'), table_name='ledger_entries')
    op.drop_table('ledger_entries')
    op.drop_index('ix_usage_events_wallet_created', table_name='usage_events')
    op.drop_index('ix_usage_events_user_occurred', table_name='usage_events')
    op.drop_index('ix_usage_events_status_created', table_name='usage_events')
    op.drop_table('usage_events')
    op.drop_index(op.f('ix_payments_user_id'), table_name='payments')
    op.drop_index('ix_payments_topup', table_name='payments')
    op.drop_index('ix_payments_state', table_name='payments')
    op.drop_table('payments')
    op.drop_index('ix_topups_user_created', table_name='topups')
    op.drop_index('ix_topups_status_created', table_name='topups')
    op.drop_table('topups')
    op.drop_index('ix_prices_lookup', table_name='prices')
    op.drop_table('prices')
    op.drop_index('ix_ai_sessions_wallet_status', table_name='ai_sessions')
    op.drop_index('ix_ai_sessions_user_created', table_name='ai_sessions')
    op.drop_index('ix_ai_sessions_unreleased_holds', table_name='ai_sessions', postgresql_where=sa.text('hold_released_at IS NULL'), sqlite_where=sa.text('hold_released_at IS NULL'))
    op.drop_index('ix_ai_sessions_status_heartbeat', table_name='ai_sessions')
    op.drop_index(op.f('ix_ai_sessions_service_api_key_id'), table_name='ai_sessions')
    op.drop_table('ai_sessions')
    op.drop_index(op.f('ix_wallets_user_id'), table_name='wallets')
    op.drop_table('wallets')
    op.drop_index('ix_usage_daily_rollups_user_day', table_name='usage_daily_rollups')
    op.drop_index('ix_usage_daily_rollups_day', table_name='usage_daily_rollups')
    op.drop_table('usage_daily_rollups')
    op.drop_index('ix_service_api_keys_is_active', table_name='service_api_keys')
    op.drop_table('service_api_keys')
    op.drop_index('ix_price_book_versions_status_effective', table_name='price_book_versions')
    op.drop_table('price_book_versions')
    op.drop_index('ix_credit_rates_status_effective', table_name='credit_rates')
    op.drop_table('credit_rates')
