import json
import re
from dataclasses import dataclass

from aiogram.enums import MessageEntityType
from aiogram.types import Message


CODE_PLACEHOLDER = "[CODE]"
_EMOJI_PART = "__EMOJI_PART__"

CODE_ENTITY_TYPES = {MessageEntityType.CODE, MessageEntityType.PRE}
LINK_ENTITY_TYPES = {MessageEntityType.URL, MessageEntityType.TEXT_LINK}

STANDALONE_URL_PATTERN = re.compile(
    r"^(?:(?:https?://|tg://|www\.)\S+|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?::\d+)?(?:[/\?#]\S*)?)$",
    flags=re.IGNORECASE,
)

CODE_LINE_PATTERN = re.compile(
    r"^\s*(?:from\s+\S+\s+import\s+|import\s+\S+|"
    r"(?:async\s+)?def\s+\w+\s*\(|class\s+\w+|"
    r"(?:const|let|var)\s+\w+\s*=|function\s+\w*\s*\(|"
    r"(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b|"
    r"(?:GET|POST|PUT|PATCH|DELETE)\s+/\S+|"
    r"Traceback \(most recent call last\)|File \".+\", line \d+|"
    r"<\?xml\b|<!DOCTYPE\s+html\b|<html\b)",
    flags=re.IGNORECASE | re.MULTILINE,
)

EMOJI_PATTERN = re.compile(
    r"(?:"
    r"[\U0001F1E6-\U0001F1FF]{2}|"
    r"[0-9#*]\ufe0f?\u20e3|"
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF]"
    r"(?:\ufe0f|\ufe0e)?"
    r"[\U0001F3FB-\U0001F3FF]?"
    r"(?:\u200d[\U0001F300-\U0001FAFF\u2600-\u27BF]"
    r"(?:\ufe0f|\ufe0e)?[\U0001F3FB-\U0001F3FF]?)*"
    r")+"
)


@dataclass(frozen=True, slots=True)
class ContentPart:
    text: str
    translatable: bool
    placeholder: str | None = None


def message_text(message: Message) -> str:
    return getattr(message, "text", None) or getattr(message, "caption", None) or ""


def message_entities(message: Message) -> list:
    return list(
        getattr(message, "entities", None)
        or getattr(message, "caption_entities", None)
        or []
    )


def media_placeholder(message: Message) -> str | None:
    if getattr(message, "photo", None):
        return "[IMG]"
    if getattr(message, "sticker", None):
        return "[STICKER]"
    if getattr(message, "animation", None):
        return "[GIF]"
    if getattr(message, "video", None) or getattr(message, "video_note", None):
        return "[VIDEO]"
    if getattr(message, "voice", None):
        return "[VOICE]"
    if getattr(message, "audio", None):
        return "[AUDIO]"
    if getattr(message, "document", None):
        return "[FILE]"
    return None


def is_json(text: str) -> bool:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, (dict, list))


def is_standalone_link(message: Message) -> bool:
    text = message_text(message)
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
        for entity in message_entities(message)
    )


def _utf16_slice(text: str, start: int, end: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[start * 2:end * 2].decode("utf-16-le")


def _merge_parts(parts: list[ContentPart]) -> list[ContentPart]:
    merged: list[ContentPart] = []
    for part in parts:
        if not part.text:
            continue
        if (
            merged
            and merged[-1].translatable == part.translatable
            and merged[-1].placeholder == part.placeholder
        ):
            previous = merged[-1]
            merged[-1] = ContentPart(
                previous.text + part.text,
                part.translatable,
                part.placeholder,
            )
        else:
            merged.append(part)
    return merged


def _split_by_entities(message: Message, text: str) -> list[ContentPart]:
    ranges = []
    for entity in message_entities(message):
        if entity.type in CODE_ENTITY_TYPES:
            placeholder = CODE_PLACEHOLDER
        elif entity.type == MessageEntityType.CUSTOM_EMOJI:
            placeholder = _EMOJI_PART
        else:
            continue
        ranges.append((entity.offset, entity.offset + entity.length, placeholder))

    if not ranges:
        return [ContentPart(text, True)]

    ranges.sort(key=lambda item: item[0])
    total_units = len(text.encode("utf-16-le")) // 2
    cursor = 0
    parts: list[ContentPart] = []
    for start, end, placeholder in ranges:
        if start < cursor:
            continue
        parts.append(ContentPart(_utf16_slice(text, cursor, start), True))
        parts.append(ContentPart(_utf16_slice(text, start, end), False, placeholder))
        cursor = end
    parts.append(ContentPart(_utf16_slice(text, cursor, total_units), True))
    return _merge_parts(parts)


def looks_like_code(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return True
    if CODE_LINE_PATTERN.search(text):
        return True
    lines = [line for line in text.splitlines() if line.strip()]
    indented_lines = sum(line.startswith(("    ", "\t")) for line in lines)
    technical_characters = sum(text.count(char) for char in "{}[]();=<>")
    return (
        len(lines) >= 2
        and indented_lines >= 1
        and technical_characters >= 4
    ) or technical_characters >= 12


def _expand_emojis(part: ContentPart) -> list[ContentPart]:
    if not part.translatable:
        return [part]
    result: list[ContentPart] = []
    cursor = 0
    for match in EMOJI_PATTERN.finditer(part.text):
        result.append(ContentPart(part.text[cursor:match.start()], True))
        result.append(ContentPart(match.group(), False, _EMOJI_PART))
        cursor = match.end()
    result.append(ContentPart(part.text[cursor:], True))
    return result


def _process_emojis(parts: list[ContentPart]) -> list[ContentPart]:
    """Удаляет внутренние emoji и сохраняет emoji на границах сообщения."""
    expanded: list[ContentPart] = []
    for part in parts:
        expanded.extend(_expand_emojis(part))

    result: list[ContentPart] = []
    for index, part in enumerate(expanded):
        if part.placeholder != _EMOJI_PART:
            result.append(part)
            continue

        has_content_before = any(
            candidate.translatable
            and candidate.text.strip()
            for candidate in expanded[:index]
        )
        has_content_after = any(
            candidate.translatable
            and candidate.text.strip()
            for candidate in expanded[index + 1:]
        )

        if not has_content_before or not has_content_after:
            result.append(
                ContentPart(part.text, False, part.text)
            )

    return _merge_parts(result)


def split_content(message: Message) -> list[ContentPart]:
    text = message_text(message)
    if not text:
        return []
    if is_standalone_link(message):
        return [ContentPart(text, False)]
    if is_json(text):
        return [ContentPart(text, False)]

    entity_parts = _split_by_entities(message, text)
    parts: list[ContentPart] = []
    for entity_part in entity_parts:
        if not entity_part.translatable:
            parts.append(entity_part)
            continue
        inside_fence = False
        for line in entity_part.text.splitlines(keepends=True):
            if line.strip().startswith("```"):
                inside_fence = not inside_fence
                parts.append(ContentPart(line, False, CODE_PLACEHOLDER))
            else:
                line_part = ContentPart(
                    line,
                    not inside_fence and not looks_like_code(line),
                    CODE_PLACEHOLDER if inside_fence or looks_like_code(line) else None,
                )
                parts.append(line_part)

    marker = media_placeholder(message)
    if marker:
        parts.insert(0, ContentPart(marker + "\n", False, marker))
    return _process_emojis(_merge_parts(parts))


def should_translate(message: Message) -> bool:
    return any(
        part.translatable and part.text.strip()
        for part in split_content(message)
    )
