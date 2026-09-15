"""Boot-time refusals, and the defaults they exist to catch.

`assert_production_ready` is the last thing between a copied `.env` and a
production process running on development settings. What it checks is
therefore a security decision, not a tidiness one, and each check here names
the leak it prevents.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def _production(**overrides) -> Settings:
    """A settings object that passes every check except the one under test."""
    base = dict(
        environment="production",
        debug=False,
        jwt_secret="a" * 64,
        internal_key_secret="b" * 64,
        database_url="postgresql+asyncpg://u:p@localhost/synora_api",
    )
    base.update(overrides)
    return Settings(**base)


def test_debug_is_off_unless_it_is_asked_for():
    """The default is the fix.

    `debug` is what turns SQLAlchemy's `echo` on, which writes every statement
    to the journal — the password and OTP hash columns among them. It
    defaulted true, which was harmless only for as long as production stayed
    on SQLite, where `echo` is suppressed anyway. The Postgres migration guide
    never named this as a line to change, so the safe value has to be the one
    nobody has to remember.

    Asserted against the declared default rather than `Settings().debug`:
    `conftest` exports `DEBUG=false` before this package imports anything, and
    in pydantic-settings an environment variable outranks a field default — so
    the constructed value reads back false no matter what the class says, and
    a test asserting on it would pass with the bug reinstated.
    """
    assert Settings.model_fields["debug"].default is False


def test_production_refuses_to_boot_with_debug_on():
    _production().assert_production_ready()  # the control: this must not raise

    with pytest.raises(RuntimeError) as caught:
        _production(debug=True).assert_production_ready()

    message = str(caught.value)
    assert "DEBUG" in message
    # The refusal has to say what leaks, or the operator turns it back on.
    assert "password" in message.lower()
