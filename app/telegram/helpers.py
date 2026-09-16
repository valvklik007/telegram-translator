import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions, Message, ReplyParameters

from app.config import Settings

logger = logging.getLogger(__name__)


def message_is_allowed(message: Message, settings: Settings) -> bool:
    if settings.allowed_chat_id == 0:
        return False
    return message.chat.id == settings.allowed_chat_id


async def send_translation_message(
    bot: Bot, source_message: Message, text: str
) -> Message:
    return await bot.send_message(
        chat_id=source_message.chat.id,
        text=text,
        message_thread_id=source_message.message_thread_id,
        reply_parameters=ReplyParameters(
            message_id=source_message.message_id,
            allow_sending_without_reply=True,
        ),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def sync_translation_messages(
    bot: Bot,
    source_message: Message,
    old_message_ids: list[int],
    new_parts: list[str],
) -> list[int]:
    """Редактирует существующие ответы, удаляя лишние и создавая недостающие."""
    final_ids: list[int] = []
    for index, text in enumerate(new_parts):
        if index < len(old_message_ids):
            old_message_id = old_message_ids[index]
            try:
                await bot.edit_message_text(
                    chat_id=source_message.chat.id,
                    message_id=old_message_id,
                    text=text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                final_ids.append(old_message_id)
                continue
            except TelegramBadRequest as exc:
                error_text = str(exc).lower()
                if "message is not modified" in error_text:
                    final_ids.append(old_message_id)
                    continue
                logger.warning("Could not edit message %s: %s", old_message_id, exc)

        sent = await send_translation_message(
            bot=bot, source_message=source_message, text=text
        )
        final_ids.append(sent.message_id)

    for old_message_id in old_message_ids[len(new_parts):]:
        try:
            await bot.delete_message(
                chat_id=source_message.chat.id, message_id=old_message_id
            )
        except TelegramBadRequest:
            pass
    return final_ids
