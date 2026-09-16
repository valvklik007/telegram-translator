import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings
from app.repositories.message_repository import Storage
from app.services.translation import TranslationService
from app.telegram.content_filter import should_translate
from app.telegram.content_translation import translate_message_content
from app.telegram.formatting import build_translation_messages
from app.telegram.helpers import message_is_allowed, sync_translation_messages

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("chatid"))
async def get_chat_id(message: Message) -> None:
    await message.reply(f"Chat ID:\n{message.chat.id}")


@router.message(F.text)
async def handle_new_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    storage: Storage,
    translator_service: TranslationService,
) -> None:
    if not message_is_allowed(message, settings):
        return
    if message.text.startswith("/"):
        return
    if message.from_user and message.from_user.is_bot:
        return
    if not should_translate(message):
        return
    text = message.text.strip()
    if not text:
        return
    user_id = message.from_user.id if message.from_user else None
    claimed = await storage.claim_message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=user_id,
        text=text,
    )
    if not claimed:
        logger.info(
            "Duplicate message ignored: chat=%s message=%s",
            message.chat.id,
            message.message_id,
        )
        return

    try:
        link = await storage.get_message(message.chat.id, message.message_id)
        if not link:
            return
        translated = await translate_message_content(
            message,
            translator_service,
        )
        parts = build_translation_messages(translated)
        bot_message_ids = await sync_translation_messages(
            bot=bot,
            source_message=message,
            old_message_ids=[],
            new_parts=parts,
        )
        await storage.set_bot_messages(
            chat_id=message.chat.id,
            source_message_id=message.message_id,
            bot_message_ids=bot_message_ids,
        )

        latest = await storage.get_message(message.chat.id, message.message_id)
        if latest and latest.source_text != link.source_text:
            translated = await translate_message_content(
                message,
                translator_service,
                text=latest.source_text,
            )
            parts = build_translation_messages(translated)
            bot_message_ids = await sync_translation_messages(
                bot=bot,
                source_message=message,
                old_message_ids=bot_message_ids,
                new_parts=parts,
            )
            await storage.set_bot_messages(
                chat_id=message.chat.id,
                source_message_id=message.message_id,
                bot_message_ids=bot_message_ids,
            )
    except Exception:
        logger.exception("Failed to translate message %s", message.message_id)


@router.edited_message(F.text)
async def handle_edited_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    storage: Storage,
    translator_service: TranslationService,
) -> None:
    if not message_is_allowed(message, settings):
        return
    if message.from_user and message.from_user.is_bot:
        return
    if not should_translate(message):
        return
    text = message.text.strip()
    if not text:
        return
    link, changed = await storage.update_source_text(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
    )
    if link is None or not changed or not link.bot_message_ids:
        return

    try:
        translated = await translate_message_content(
            message,
            translator_service,
        )
        parts = build_translation_messages(translated)
        bot_message_ids = await sync_translation_messages(
            bot=bot,
            source_message=message,
            old_message_ids=link.bot_message_ids,
            new_parts=parts,
        )
        await storage.set_bot_messages(
            chat_id=message.chat.id,
            source_message_id=message.message_id,
            bot_message_ids=bot_message_ids,
        )
        logger.info(
            "Translation updated: chat=%s message=%s",
            message.chat.id,
            message.message_id,
        )
    except Exception:
        logger.exception(
            "Failed to update translation for message %s", message.message_id
        )
