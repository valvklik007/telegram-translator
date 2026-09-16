import json
import re
from dataclasses import dataclass

from aiogram.enums import MessageEntityType
from aiogram.types import Message


CODE_ENTITY_TYPES = {
    MessageEntityType.CODE,
    MessageEntityType.PRE,
}

LINK_ENTITY_TYPES = {
    MessageEntityType.URL,
    MessageEntityType.TEXT_LINK,
}

STANDALONE_URL_PATTERN = re.compile(
    r"^(?:(?:https?://|tg://|www\.)\S+|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?::\d+)?(?:[/\?#]\S*)?)$",
    flags=re.IGNORECASE,
)

CODE_LINE_PATTERN = re.compile(
    r"^\s*(?:"
    r"from\s+\S+\s+import\s+|"
    r"import\s+\S+|"
    r"(?:async\s+)?def\s+\w+\s*\(|"
    r"class\s+\w+|"
    r"(?:const|let|var)\s+\w+\s*=|"
    r"function\s+\w*\s*\(|"
    r"(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b|"
    r"(?:GET|POST|PUT|PATCH|DELETE)\s+/\S+|"
    r"Traceback \(most recent call last\)|"
    r"File \".+\", line \d+|"
    r"<\?xml\b|<!DOCTYPE\s+html\b|<html\b"
    r")",
    flags=re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class ContentPart:
    text: str
    translatable: bool


def is_json(text: str) -> bool:
    """Определяет JSON-объекты и массивы, не считая JSON обычной строкой."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False

    return isinstance(value, (dict, list))


def has_code_entity(message: Message) -> bool:
    """Проверяет нативную Telegram-разметку `code` и `pre`."""
    return any(
        entity.type in CODE_ENTITY_TYPES
        for entity in (message.entities or [])
    )


def is_standalone_link(message: Message) -> bool:
    """Определяет сообщение, состоящее только из одной ссылки."""
    text = message.text or ""
    stripped = text.strip()
    if not stripped:
        return False

    if STANDALONE_URL_PATTERN.fullmatch(stripped):
        return True

    leading_units = len(text[:len(text) - len(text.lstrip())].encode("utf-16-le")) // 2
    stripped_units = len(stripped.encode("utf-16-le")) // 2
    return any(
        entity.type in LINK_ENTITY_TYPES
        and entity.offset == leading_units
        and entity.length == stripped_units
        for entity in (message.entities or [])
    )


def _utf16_slice(text: str, start: int, end: int) -> str:
    """Вырезает текст по UTF-16 offsets, которые использует Telegram."""
    encoded = text.encode("utf-16-le")
    return encoded[start * 2:end * 2].decode("utf-16-le")


def _merge_parts(parts: list[ContentPart]) -> list[ContentPart]:
    merged: list[ContentPart] = []
    for part in parts:
        if not part.text:
            continue
        if merged and merged[-1].translatable == part.translatable:
            previous = merged[-1]
            merged[-1] = ContentPart(
                text=previous.text + part.text,
                translatable=part.translatable,
            )
        else:
            merged.append(part)
    return merged


def _split_by_code_entities(message: Message) -> list[ContentPart]:
    text = message.text or ""
    code_ranges = sorted(
        (
            entity.offset,
            entity.offset + entity.length,
        )
        for entity in (message.entities or [])
        if entity.type in CODE_ENTITY_TYPES
    )

    if not code_ranges:
        return []

    merged_ranges: list[tuple[int, int]] = []
    for start, end in code_ranges:
        if merged_ranges and start <= merged_ranges[-1][1]:
            old_start, old_end = merged_ranges[-1]
            merged_ranges[-1] = old_start, max(old_end, end)
        else:
            merged_ranges.append((start, end))

    total_units = len(text.encode("utf-16-le")) // 2
    cursor = 0
    parts: list[ContentPart] = []
    for start, end in merged_ranges:
        parts.append(ContentPart(_utf16_slice(text, cursor, start), True))
        parts.append(ContentPart(_utf16_slice(text, start, end), False))
        cursor = end
    parts.append(ContentPart(_utf16_slice(text, cursor, total_units), True))
    return _merge_parts(parts)


def looks_like_code(text: str) -> bool:
    """Находит цельные блоки кода по консервативным признакам."""
    stripped = text.strip()

    if stripped.startswith("```") and stripped.endswith("```"):
        return True

    if CODE_LINE_PATTERN.search(text):
        return True

    lines = [line for line in text.splitlines() if line.strip()]
    indented_lines = sum(
        line.startswith(("    ", "\t"))
        for line in lines
    )
    technical_characters = sum(
        text.count(character)
        for character in "{}[]();=<>"
    )

    return (
        len(lines) >= 2
        and indented_lines >= 1
        and technical_characters >= 4
    ) or technical_characters >= 12


def split_content(message: Message) -> list[ContentPart]:
    """Разделяет сообщение на переводимый текст и неизменяемый код."""
    text = message.text or ""
    if not text:
        return []

    if is_standalone_link(message):
        return [ContentPart(text, False)]

    entity_parts = _split_by_code_entities(message)
    if entity_parts:
        return entity_parts

    if is_json(text):
        return [ContentPart(text, False)]

    parts: list[ContentPart] = []
    inside_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            inside_fence = not inside_fence
            parts.append(ContentPart(line, False))
            continue

        parts.append(
            ContentPart(
                text=line,
                translatable=(
                    not inside_fence
                    and not looks_like_code(line)
                ),
            )
        )

    return _merge_parts(parts)


def should_translate(message: Message) -> bool:
    """Проверяет, есть ли в сообщении хотя бы один обычный текстовый фрагмент."""
    return any(
        part.translatable and part.text.strip()
        for part in split_content(message)
    )
