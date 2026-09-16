import re

from aiogram.types import Message

from app.languages import TARGET_LANGUAGES
from app.services.translation import TranslationService
from app.telegram.content_filter import message_text, split_content


def _surrounding_whitespace(text: str) -> tuple[str, str]:
    leading = text[:len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):]
    return leading, trailing


async def translate_message_content(
    message: Message,
    translator_service: TranslationService,
    text: str | None = None,
) -> dict[str, str]:
    """Переводит обычный текст, сохраняя фрагменты кода без изменений."""
    content_message = message
    if text is not None and text != message_text(message):
        content_message = message.model_copy(
            update={
                "text": text,
                "caption": None,
                "entities": [],
                "caption_entities": [],
            }
        )

    translations = {
        language: ""
        for language in TARGET_LANGUAGES
    }

    for part in split_content(content_message):
        if not part.text.strip():
            for language in translations:
                translations[language] += part.text
            continue

        leading_space, trailing_space = _surrounding_whitespace(part.text)

        if not part.translatable:
            for language in translations:
                translations[language] += (
                    leading_space
                    + (part.placeholder or "[CONTENT]")
                    + trailing_space
                )
            continue

        text_to_translate = re.sub(
            r"[ \t]{2,}",
            " ",
            part.text.strip(),
        )
        translated_part = await translator_service.translate(text_to_translate)

        for language in translations:
            translations[language] += (
                leading_space
                + translated_part[language]
                + trailing_space
            )

    return translations
