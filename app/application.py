import logging

from aiogram import Bot, Dispatcher

from app.config import Settings
from app.repositories.message_repository import Storage
from app.services.translation import TranslationService
from app.telegram.handlers import router

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings.from_env()
    storage = Storage(settings.db_path)
    await storage.connect()

    translator_service = TranslationService()
    await translator_service.start()

    bot = Bot(token=settings.telegram_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        if settings.drop_pending_updates:
            await bot.delete_webhook(drop_pending_updates=True)

        me = await bot.get_me()
        logger.info("Bot started: @%s", me.username)
        logger.info("Allowed chat ID: %s", settings.allowed_chat_id)

        await dispatcher.start_polling(
            bot,
            settings=settings,
            storage=storage,
            translator_service=translator_service,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await translator_service.close()
        await storage.close()
        await bot.session.close()
