"""Metering and prepaid credit.

The one rule worth knowing before reading anything else in here:
`wallet_repo` is the only module that mutates a wallet balance. Everything
else — sessions, usage ingest, top-ups, admin grants — goes through it, in one
transaction, so that

    wallets.<bucket>_micros == SUM(ledger_entries.amount_micros for that bucket)

is true by construction rather than by discipline. `tests/test_billing_invariants.py`
walks the source tree to keep it that way.
"""
