from app.languages import TARGET_LANGUAGES


def split_text(text: str, max_length: int) -> list[str]:
    """Делит длинный текст с учётом лимита Telegram."""
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        split_position = remaining.rfind("\n", 0, max_length)
        if split_position < max_length // 2:
            split_position = remaining.rfind(" ", 0, max_length)
        if split_position < max_length // 2:
            split_position = max_length
        chunks.append(remaining[:split_position].strip())
        remaining = remaining[split_position:].strip()
    return chunks


def build_translation_messages(translations: dict[str, str]) -> list[str]:
    """Собирает переводы в одно или несколько Telegram-сообщений."""
    units: list[str] = []
    for language in TARGET_LANGUAGES:
        translated = translations.get(language)
        if not translated:
            continue
        flag, title = TARGET_LANGUAGES[language]
        chunks = split_text(translated, max_length=3400)
        for index, chunk in enumerate(chunks):
            suffix = "" if index == 0 else " — продолжение"
            units.append(f"{flag} {title}{suffix}\n{chunk}")

    result: list[str] = []
    current = ""
    for unit in units:
        candidate = unit if not current else f"{current}\n\n{unit}"
        if len(candidate) <= 3900:
            current = candidate
            continue
        if current:
            result.append(current)
        current = unit
    if current:
        result.append(current)
    return result
