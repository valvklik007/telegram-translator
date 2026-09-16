import asyncio
import logging
from typing import Optional

import free_deepl_translator as deepl

from app.languages import TARGET_LANGUAGES

logger = logging.getLogger(__name__)


class TranslationService:
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
        if self.translator is None:
            return
        try:
            await self.translator.CloseAsync()
            await asyncio.sleep(0.3)
        finally:
            self.translator = None
        logger.info("DeepL translation session closed")

    async def translate(self, text: str) -> dict[str, str]:
        """Переводит сообщение на RU / EN / CS с автоопределением языка."""
        if self.translator is None:
            await self.start()
        async with self.semaphore:
            translations: dict[str, str] = {}
            for language in TARGET_LANGUAGES:
                translations[language] = await self._translate_one(
                    text=text, target_language=language
                )
            return translations

    async def _translate_one(self, text: str, target_language: str) -> str:
        if self.translator is None:
            raise RuntimeError("DeepL session is not initialized")
        result = await self.translator.TranslateAsync(
            text, target_lang=target_language
        )
        if result.get("status") != 0:
            raise RuntimeError(
                f"DeepL error: {result.get('msg', 'Unknown error')}"
            )
        translated_text = result.get("text")
        if not translated_text:
            raise RuntimeError("DeepL returned empty translation")
        return translated_text
