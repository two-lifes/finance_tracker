"""Minimal stand-ins for the Telegram network layer, so dispatcher flows run offline."""

import itertools
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Message,
    Sticker,
    Update,
    User,
)

OWNER_ID = 1000
STRANGER_ID = 2000

_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        # Set to an exception to make the next request fail with it
        self.fail_next: Exception | None = None

    # Signature is fixed by aiogram's BaseSession
    async def make_request(
        self, bot: Bot, method: TelegramMethod, timeout: int | None = None  # noqa: ASYNC109
    ):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        # answerCallbackQuery, editMessageText, editMessageReplyMarkup, ...
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):
        yield b""

    async def close(self) -> None:
        pass

    def texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, SendMessage | EditMessageText)]

    def last_text(self) -> str:
        return self.texts()[-1]

    def sent(self) -> list[SendMessage]:
        return [m for m in self.requests if isinstance(m, SendMessage)]

    def callback_answers(self) -> list[str | None]:
        return [m.text for m in self.requests if isinstance(m, AnswerCallbackQuery)]


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _message(user_id: int, text: str) -> Message:
    return Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=_user(user_id),
        text=text,
    )


def text_update(text: str, user_id: int = OWNER_ID) -> Update:
    return Update(update_id=next(_ids), message=_message(user_id, text))


def callback_update(data: str, user_id: int = OWNER_ID, inaccessible: bool = False) -> Update:
    # Telegram sends InaccessibleMessage for messages the bot can no longer read or edit
    message = (
        InaccessibleMessage(chat=Chat(id=user_id, type="private"), message_id=next(_ids))
        if inaccessible
        else _message(user_id, "previous bot message")
    )
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)),
            from_user=_user(user_id),
            chat_instance="test",
            data=data,
            message=message,
        ),
    )


def sticker_update(user_id: int = OWNER_ID) -> Update:
    message = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=_user(user_id),
        sticker=Sticker(
            file_id="s", file_unique_id="s", type="regular", width=1, height=1,
            is_animated=False, is_video=False,
        ),
    )
    return Update(update_id=next(_ids), message=message)
