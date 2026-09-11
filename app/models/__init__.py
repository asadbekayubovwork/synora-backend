"""Every mapped model, re-exported.

`init_db()` and Alembic's autogenerate both import this module purely for the
side effect of registering the mappers on `Base.metadata`. A model that is not
reachable from here is invisible to both: its table is never created in tests,
and autogenerate proposes dropping it in production.
"""

from app.models.ai_session import AiSession
from app.models.billing_enums import (
    AiSessionKind,
    AiSessionStatus,
    BillingService,
    CreditRateStatus,
    LedgerBucket,
    LedgerEntryKind,
    LedgerRefType,
    PaymentState,
    PriceBookStatus,
    RoundingMode,
    SessionAction,
    SessionEndReason,
    TopupProvider,
    TopupStatus,
    TtsBatchJobState,
    UsageEventKind,
    UsageEventStatus,
    UsageMetric,
)
from app.models.credit_rate import CreditRate
from app.models.ledger import LedgerEntry
from app.models.oauth import OAuthAccount, OAuthProviderName
from app.models.otp import OtpCode, OtpPurpose
from app.models.price_book import MODEL_KEY_ANY, Price, PriceBookVersion
from app.models.service_api_key import ServiceApiKey
from app.models.topup import Payment, Topup
from app.models.tts_job import TtsBatchJob
from app.models.tts_recording import TtsRecording
from app.models.usage import UsageEvent, UsageEventItem
from app.models.usage_rollup import UsageDailyRollup
from app.models.user import User, normalize_email
from app.models.wallet import Wallet

__all__ = [
    "MODEL_KEY_ANY",
    "AiSession",
    "AiSessionKind",
    "AiSessionStatus",
    "BillingService",
    "CreditRate",
    "CreditRateStatus",
    "LedgerBucket",
    "LedgerEntry",
    "LedgerEntryKind",
    "LedgerRefType",
    "OAuthAccount",
    "OAuthProviderName",
    "OtpCode",
    "OtpPurpose",
    "Payment",
    "PaymentState",
    "Price",
    "PriceBookStatus",
    "PriceBookVersion",
    "RoundingMode",
    "ServiceApiKey",
    "SessionAction",
    "SessionEndReason",
    "Topup",
    "TopupProvider",
    "TopupStatus",
    "TtsBatchJob",
    "TtsBatchJobState",
    "TtsRecording",
    "UsageDailyRollup",
    "UsageEvent",
    "UsageEventItem",
    "UsageEventKind",
    "UsageEventStatus",
    "UsageMetric",
    "User",
    "Wallet",
    "normalize_email",
]
