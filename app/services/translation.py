import asyncio
import logging
from typing import Optional

import free_deepl_translator as deepl

from app.languages import TARGET_LANGUAGES

logger = logging.getLogger(__name__)


class TranslationSessionError(RuntimeError):
    """Соединение с DeepL потеряно и может быть восстановлено повтором."""


class TranslationService:
    SESSION_RETRY_DELAY = 0.5

    def __init__(self):
        self.semaphore = asyncio.Semaphore(1)
        self.translator: Optional[deepl.DeeplTranslator] = None
        self.start_lock = asyncio.Lock()

    async def start(self) -> None:
        """Создаёт одну DeepL-сессию на всё время работы приложения."""
        async with self.start_lock:
            if self.translator is not None:
                return
            translator = deepl.DeeplTranslator()
            session_created = await translator.SessionAsync()
            if session_created is not True:
                raise RuntimeError("Не удалось создать DeepL session")
            self.translator = translator
            logger.info("DeepL translation session started")

    async def close(self) -> None:
        """Закрывает WebSocket только при остановке приложения."""
        translator = self.translator
        self.translator = None

        if translator is None:
            return

        try:
            await translator.CloseAsync()
            await asyncio.sleep(0.3)
        except Exception:
            logger.warning(
                "Could not close DeepL translation session cleanly",
                exc_info=True,
            )
        logger.info("DeepL translation session closed")

    async def translate(self, text: str) -> dict[str, str]:
        """Переводит сообщение на RU / EN / CS с автоопределением языка."""
        async with self.semaphore:
            for attempt in range(2):
                if self.translator is None:
                    await self.start()

                try:
                    return await self._translate_all(text)
                except TranslationSessionError:
                    logger.warning(
                        "DeepL session is invalid; reconnecting "
                        "(attempt %s/2)",
                        attempt + 1,
                    )
                    await self.close()

                    if attempt == 1:
                        raise

                    await asyncio.sleep(self.SESSION_RETRY_DELAY)

            raise RuntimeError("DeepL translation retry loop ended unexpectedly")

    async def _translate_all(self, text: str) -> dict[str, str]:
        translations: dict[str, str] = {}
        for language in TARGET_LANGUAGES:
            translations[language] = await self._translate_one(
                text=text,
                target_language=language,
            )
        return translations

    async def _translate_one(self, text: str, target_language: str) -> str:
        if self.translator is None:
            raise RuntimeError("DeepL session is not initialized")
        try:
            result = await self.translator.TranslateAsync(
                text,
                target_lang=target_language,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise TranslationSessionError(
                f"DeepL connection failed: {exc}"
            ) from exc

        if result.get("status") != 0:
            error_message = result.get("msg", "Unknown error")
            normalized_error = error_message.lower()
            session_error_markers = (
                "invalid session",
                "not connected",
                "connection closed",
                "connection failed",
                "timed out",
                "timeout",
            )

            if any(
                marker in normalized_error
                for marker in session_error_markers
            ):
                raise TranslationSessionError(
                    f"DeepL error: {error_message}"
                )

            raise RuntimeError(f"DeepL error: {error_message}")
        translated_text = result.get("text")
        if not translated_text:
            raise RuntimeError("DeepL returned empty translation")
        return translated_text
