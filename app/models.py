from dataclasses import dataclass


@dataclass(slots=True)
class MessageLink:
    source_text: str
    bot_message_ids: list[int]
