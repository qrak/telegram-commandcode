"""Gateway state: rate limiting, chat locks, bot identity caching."""

from telegram_commandcode.gateway import gateway as gateway_mod
from telegram_commandcode.gateway.gateway import BotGateway


def test_chat_lock_is_stable_per_chat():
    gw = BotGateway()
    assert gw.get_chat_lock("1") is gw.get_chat_lock("1")
    assert gw.get_chat_lock("1") is not gw.get_chat_lock("2")


def test_rate_limit_blocks_second_message_inside_window(monkeypatch):
    gw = BotGateway()
    now = 100.0
    monkeypatch.setattr(gateway_mod.time, "monotonic", lambda: now)

    assert gw.check_rate_limit(7) is True
    assert gw.check_rate_limit(7) is False

    now += gw.RATE_LIMIT_WINDOW + 0.1
    assert gw.check_rate_limit(7) is True


def test_rate_limit_is_per_user(monkeypatch):
    gw = BotGateway()
    monkeypatch.setattr(gateway_mod.time, "monotonic", lambda: 100.0)
    assert gw.check_rate_limit(1) is True
    assert gw.check_rate_limit(2) is True


def test_rate_limit_ignores_missing_user():
    assert BotGateway().check_rate_limit(None) is True


async def test_identity_is_cached():
    class Bot:
        calls = 0

        async def get_me(self):
            Bot.calls += 1

            class Me:
                username = "bot"
                id = 42

            return Me()

    gw = BotGateway()
    bot = Bot()
    await gw.ensure_bot_identity(bot)
    await gw.ensure_bot_identity(bot)

    assert gw.bot_username == "bot"
    assert gw.bot_id == 42
    assert Bot.calls == 1


async def test_identity_failure_does_not_raise():
    class BrokenBot:
        async def get_me(self):
            raise RuntimeError("network down")

    gw = BotGateway()
    await gw.ensure_bot_identity(BrokenBot())
    assert gw.bot_username is None
