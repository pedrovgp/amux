import asyncio
import logging

from telegram_gateway.guard import Guard, allow_only


class FakeUser:
    def __init__(self, user_id, username=None):
        self.id = user_id
        self.username = username


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self):
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeUpdate:
    def __init__(self, user_id, username=None, chat_id=10, callback=False):
        self.effective_user = FakeUser(user_id, username)
        self.effective_chat = FakeChat(chat_id)
        self.effective_message = FakeMessage()
        self.callback_query = FakeQuery() if callback else None


class FakeApplication:
    def __init__(self, guard):
        self.bot_data = {"guard": guard}


class FakeContext:
    def __init__(self, guard):
        self.application = FakeApplication(guard)


def test_is_allowed():
    guard = Guard(frozenset({1, 2}), logging.getLogger("t"))
    assert guard.is_allowed(1)
    assert not guard.is_allowed(3)
    assert not guard.is_allowed(None)


def test_denial_is_logged(caplog):
    logger = logging.getLogger("test.guard")
    guard = Guard(frozenset({1}), logger)
    with caplog.at_level(logging.WARNING, logger="test.guard"):
        guard.record_denial(user_id=99, username="intruder", chat_id=5, action="on_text")
    assert "security_denied" in caplog.text
    assert "user_id=99" in caplog.text
    assert "intruder" in caplog.text


def test_allow_only_denies_a_message_and_replies():
    guard = Guard(frozenset({1}), logging.getLogger("t"))
    calls = []

    @allow_only
    async def handler(update, context):
        calls.append("ran")

    update = FakeUpdate(user_id=99, chat_id=5)
    asyncio.run(handler(update, FakeContext(guard)))
    assert calls == []
    assert update.effective_message.replies == ["⛔ Access denied. This bot is private."]


def test_allow_only_denies_a_callback_with_an_alert():
    guard = Guard(frozenset({1}), logging.getLogger("t"))

    @allow_only
    async def handler(update, context):
        raise AssertionError("must not run")

    update = FakeUpdate(user_id=99, callback=True)
    asyncio.run(handler(update, FakeContext(guard)))
    assert update.callback_query.answers == [
        ("⛔ Access denied. This bot is private.", True)
    ]


def test_allow_only_runs_for_an_allowed_user():
    guard = Guard(frozenset({1}), logging.getLogger("t"))
    seen = []

    @allow_only
    async def handler(update, context):
        seen.append(update.effective_user.id)
        return "state"

    update = FakeUpdate(user_id=1)
    assert asyncio.run(handler(update, FakeContext(guard))) == "state"
    assert seen == [1]
