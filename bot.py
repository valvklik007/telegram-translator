import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message, ReplyParameters, LinkPreviewOptions

from dotenv import load_dotenv
from googletrans import Translator


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


router = Router()


TARGET_LANGUAGES = {
    "ru": ("🇷🇺", "Русский"),
    "en": ("🇬🇧", "English"),
    "cs": ("🇨🇿", "Čeština"),
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Settings:
    telegram_token: str
    allowed_chat_id: int
    drop_pending_updates: bool
    db_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN")

        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not configured"
            )

        return cls(
            telegram_token=token,
            allowed_chat_id=int(
                os.getenv("ALLOWED_CHAT_ID", "0")
            ),
            drop_pending_updates=(
                os.getenv(
                    "DROP_PENDING_UPDATES",
                    "true",
                ).lower()
                in {"1", "true", "yes"}
            ),
            db_path=os.getenv(
                "DB_PATH",
                "translator.db",
            ),
        )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MessageLink:
    source_text: str
    bot_message_ids: list[int]


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)

        await self.db.execute(
            """
            PRAGMA journal_mode = WAL
            """
        )

        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS message_links (
                chat_id             INTEGER NOT NULL,
                source_message_id   INTEGER NOT NULL,
                source_user_id      INTEGER,
                source_text         TEXT NOT NULL,
                bot_message_ids     TEXT NOT NULL DEFAULT '[]',

                created_at          DATETIME NOT NULL
                                    DEFAULT CURRENT_TIMESTAMP,

                updated_at          DATETIME NOT NULL
                                    DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (
                    chat_id,
                    source_message_id
                )
            )
            """
        )

        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def claim_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: Optional[int],
        text: str,
    ) -> bool:
        """
        Возвращает True только для первого обработчика сообщения.
        INSERT OR IGNORE защищает от повторных Telegram updates.
        """

        assert self.db is not None

        async with self.lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO message_links (
                    chat_id,
                    source_message_id,
                    source_user_id,
                    source_text
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    chat_id,
                    message_id,
                    user_id,
                    text,
                ),
            )

            await self.db.commit()

            return cursor.rowcount == 1

    async def get_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> Optional[MessageLink]:
        assert self.db is not None

        cursor = await self.db.execute(
            """
            SELECT
                source_text,
                bot_message_ids
            FROM message_links
            WHERE
                chat_id = ?
                AND source_message_id = ?
            """,
            (
                chat_id,
                message_id,
            ),
        )

        row = await cursor.fetchone()

        if not row:
            return None

        return MessageLink(
            source_text=row[0],
            bot_message_ids=json.loads(row[1]),
        )

    async def update_source_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> tuple[Optional[MessageLink], bool]:
        """
        Обновляет текст оригинала.

        bool показывает, действительно ли текст изменился.
        """

        assert self.db is not None

        async with self.lock:
            cursor = await self.db.execute(
                """
                SELECT
                    source_text,
                    bot_message_ids
                FROM message_links
                WHERE
                    chat_id = ?
                    AND source_message_id = ?
                """,
                (
                    chat_id,
                    message_id,
                ),
            )

            row = await cursor.fetchone()

            if not row:
                return None, False

            old_text = row[0]
            bot_message_ids = json.loads(row[1])

            if old_text == text:
                return (
                    MessageLink(
                        source_text=old_text,
                        bot_message_ids=bot_message_ids,
                    ),
                    False,
                )

            await self.db.execute(
                """
                UPDATE message_links
                SET
                    source_text = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    chat_id = ?
                    AND source_message_id = ?
                """,
                (
                    text,
                    chat_id,
                    message_id,
                ),
            )

            await self.db.commit()

            return (
                MessageLink(
                    source_text=text,
                    bot_message_ids=bot_message_ids,
                ),
                True,
            )

    async def set_bot_messages(
        self,
        chat_id: int,
        source_message_id: int,
        bot_message_ids: list[int],
    ) -> None:
        assert self.db is not None

        async with self.lock:
            await self.db.execute(
                """
                UPDATE message_links
                SET
                    bot_message_ids = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE
                    chat_id = ?
                    AND source_message_id = ?
                """,
                (
                    json.dumps(bot_message_ids),
                    chat_id,
                    source_message_id,
                ),
            )

            await self.db.commit()


# ---------------------------------------------------------------------------
# Google Translate
# ---------------------------------------------------------------------------

class TranslationService:
    def __init__(self):
        # Не даём случайному всплеску сообщений отправить
        # десятки запросов Google одновременно.
        self.semaphore = asyncio.Semaphore(5)

    async def translate(
        self,
        text: str,
    ) -> dict[str, str]:
        """
        Определяет исходный язык автоматически.

        Если исходник уже RU / EN / CS, этот язык не дублируем:
        оригинал и так виден над reply-сообщением.
        """

        last_error: Optional[Exception] = None

        for attempt in range(3):
            try:
                async with self.semaphore:
                    return await self._translate_once(text)

            except Exception as exc:
                last_error = exc

                logger.warning(
                    "Google Translate error, attempt %s/3: %s",
                    attempt + 1,
                    exc,
                )

                await asyncio.sleep(
                    0.5 * (2 ** attempt)
                )

        raise RuntimeError(
            "Google Translate failed after retries"
        ) from last_error

    async def _translate_once(
        self,
        text: str,
    ) -> dict[str, str]:
        """
        translate.googleapis.com здесь используется специально.

        googletrans умеет работать с ним без API-ключа,
        при этом не требуется web token от translate.google.com.
        """

        async with Translator(
            service_urls=[
                "translate.googleapis.com",
            ],
            raise_exception=True,
            timeout=10,
        ) as translator:

            # Первый запрос одновременно даёт нам английский
            # перевод и определённый Google исходный язык.
            en_result = await translator.translate(
                text,
                dest="en",
            )

            source_language = (
                en_result.src or ""
            ).lower()

            source_language = source_language.split("-")[0]

            translations: dict[str, str] = {}

            if source_language != "en":
                translations["en"] = en_result.text

            remaining_targets = [
                language
                for language in ("ru", "cs")
                if language != source_language
            ]

            results = await asyncio.gather(
                *[
                    translator.translate(
                        text,
                        dest=language,
                    )
                    for language in remaining_targets
                ]
            )

            for language, result in zip(
                remaining_targets,
                results,
            ):
                translations[language] = result.text

            return translations


# ---------------------------------------------------------------------------
# Telegram message formatting
# ---------------------------------------------------------------------------

def split_text(
    text: str,
    max_length: int,
) -> list[str]:
    """
    Делим длинные переводы так, чтобы не упереться
    в Telegram limit 4096 символов.
    """

    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []

    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        split_position = remaining.rfind(
            "\n",
            0,
            max_length,
        )

        if split_position < max_length // 2:
            split_position = remaining.rfind(
                " ",
                0,
                max_length,
            )

        if split_position < max_length // 2:
            split_position = max_length

        chunks.append(
            remaining[:split_position].strip()
        )

        remaining = remaining[
            split_position:
        ].strip()

    return chunks


def build_translation_messages(
    translations: dict[str, str],
) -> list[str]:
    """
    Обычно вернётся одно Telegram-сообщение.

    Для очень длинного текста автоматически разобьём
    перевод на несколько сообщений.
    """

    units: list[str] = []

    for language in ("ru", "en", "cs"):
        translated = translations.get(language)

        if not translated:
            continue

        flag, title = TARGET_LANGUAGES[language]

        chunks = split_text(
            translated,
            max_length=3400,
        )

        for index, chunk in enumerate(chunks):
            suffix = (
                ""
                if index == 0
                else " — продолжение"
            )

            units.append(
                f"{flag} {title}{suffix}\n"
                f"{chunk}"
            )

    result: list[str] = []
    current = ""

    for unit in units:
        candidate = (
            unit
            if not current
            else f"{current}\n\n{unit}"
        )

        if len(candidate) <= 3900:
            current = candidate
            continue

        if current:
            result.append(current)

        current = unit

    if current:
        result.append(current)

    return result


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def message_is_allowed(
    message: Message,
    settings: Settings,
) -> bool:
    if settings.allowed_chat_id == 0:
        return False

    return message.chat.id == settings.allowed_chat_id


async def send_translation_message(
    bot: Bot,
    source_message: Message,
    text: str,
) -> Message:
    return await bot.send_message(
        chat_id=source_message.chat.id,
        text=text,
        message_thread_id=source_message.message_thread_id,
        reply_parameters=ReplyParameters(
            message_id=source_message.message_id,
            allow_sending_without_reply=True,
        ),
        link_preview_options=LinkPreviewOptions(
            is_disabled=True,
        ),
    )


async def sync_translation_messages(
    bot: Bot,
    source_message: Message,
    old_message_ids: list[int],
    new_parts: list[str],
) -> list[int]:
    """
    Обновляет существующие сообщения бота.

    Если количество частей изменилось:
    - лишние старые удаляем;
    - недостающие отправляем.
    """

    final_ids: list[int] = []

    for index, text in enumerate(new_parts):
        if index < len(old_message_ids):
            old_message_id = old_message_ids[index]

            try:
                await bot.edit_message_text(
                    chat_id=source_message.chat.id,
                    message_id=old_message_id,
                    text=text,
                    link_preview_options=LinkPreviewOptions(
                        is_disabled=True,
                    ),
                )

                final_ids.append(old_message_id)
                continue

            except TelegramBadRequest as exc:
                error_text = str(exc).lower()

                # Это нормально, если Google вернул тот же самый перевод.
                if "message is not modified" in error_text:
                    final_ids.append(old_message_id)
                    continue

                logger.warning(
                    "Could not edit message %s: %s",
                    old_message_id,
                    exc,
                )

        sent = await send_translation_message(
            bot=bot,
            source_message=source_message,
            text=text,
        )

        final_ids.append(sent.message_id)

    # После редактирования длинный текст мог стать короче.
    for old_message_id in old_message_ids[
        len(new_parts):
    ]:
        try:
            await bot.delete_message(
                chat_id=source_message.chat.id,
                message_id=old_message_id,
            )

        except TelegramBadRequest:
            pass

    return final_ids


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@router.message(Command("chatid"))
async def get_chat_id(
    message: Message,
) -> None:
    await message.reply(
        f"Chat ID:\n{message.chat.id}"
    )


# ---------------------------------------------------------------------------
# New messages
# ---------------------------------------------------------------------------

@router.message(F.text)
async def handle_new_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    storage: Storage,
    translator_service: TranslationService,
) -> None:

    if not message_is_allowed(
        message,
        settings,
    ):
        return

    # Команды не переводим.
    if message.text.startswith("/"):
        return

    # Не переводим сообщения других ботов и самого себя.
    if (
        message.from_user
        and message.from_user.is_bot
    ):
        return

    text = message.text.strip()

    if not text:
        return

    user_id = (
        message.from_user.id
        if message.from_user
        else None
    )

    # PRIMARY KEY в SQLite не даст повторно
    # обработать один и тот же Telegram message_id.
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
        link = await storage.get_message(
            message.chat.id,
            message.message_id,
        )

        if not link:
            return

        translated = await translator_service.translate(
            link.source_text
        )

        parts = build_translation_messages(
            translated
        )

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

        # Человек мог успеть изменить сообщение,
        # пока Google выполнял перевод.
        latest = await storage.get_message(
            message.chat.id,
            message.message_id,
        )

        if (
            latest
            and latest.source_text != link.source_text
        ):
            translated = await translator_service.translate(
                latest.source_text
            )

            parts = build_translation_messages(
                translated
            )

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
        logger.exception(
            "Failed to translate message %s",
            message.message_id,
        )


# ---------------------------------------------------------------------------
# Edited messages
# ---------------------------------------------------------------------------

@router.edited_message(F.text)
async def handle_edited_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    storage: Storage,
    translator_service: TranslationService,
) -> None:

    if not message_is_allowed(
        message,
        settings,
    ):
        return

    if (
        message.from_user
        and message.from_user.is_bot
    ):
        return

    text = message.text.strip()

    if not text:
        return

    link, changed = await storage.update_source_text(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
    )

    # Значит сообщение было написано до запуска бота
    # или вообще не обрабатывалось нашим приложением.
    if link is None:
        return

    if not changed:
        return

    # Перевод первого сообщения ещё выполняется.
    # Основной handler после перевода повторно проверит source_text.
    if not link.bot_message_ids:
        return

    try:
        translated = await translator_service.translate(
            text
        )

        parts = build_translation_messages(
            translated
        )

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
            "Failed to update translation for message %s",
            message.message_id,
        )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

async def main() -> None:
    settings = Settings.from_env()

    storage = Storage(
        settings.db_path
    )

    await storage.connect()

    translator_service = TranslationService()

    bot = Bot(
        token=settings.telegram_token
    )

    dispatcher = Dispatcher()

    dispatcher.include_router(router)

    try:
        if settings.drop_pending_updates:
            # Удаляем необработанные updates перед стартом.
            # Старую переписку Telegram Bot API всё равно не отдаёт.
            await bot.delete_webhook(
                drop_pending_updates=True
            )

        me = await bot.get_me()

        logger.info(
            "Bot started: @%s",
            me.username,
        )

        logger.info(
            "Allowed chat ID: %s",
            settings.allowed_chat_id,
        )

        await dispatcher.start_polling(
            bot,
            settings=settings,
            storage=storage,
            translator_service=translator_service,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )

    finally:
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())