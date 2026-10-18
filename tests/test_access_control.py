"""Access-control tests.

The bot drives an agent that can edit files and run commands, so the default
must be fail-closed: an unset allow-list means nobody, not everybody.
"""

from telegram_commandcode.gateway import router


def test_unset_allowlist_is_empty(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    assert router.allowed_users() == set()


def test_unset_allowlist_denies_everyone(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    assert router.MessageRouter._is_allowed(12345) is False


def test_explicit_ids_are_allowed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111, 222")
    assert router.MessageRouter._is_allowed(111) is True
    assert router.MessageRouter._is_allowed(222) is True
    assert router.MessageRouter._is_allowed(333) is False


def test_any_opt_in_allows_everyone(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "any")
    assert router.MessageRouter._is_allowed(999) is True


def test_missing_user_is_never_allowed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "any")
    assert router.MessageRouter._is_allowed(None) is False


def test_whitespace_and_trailing_commas_are_tolerated(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", " 111 , ,222 ,")
    assert router.allowed_users() == {"111", "222"}
