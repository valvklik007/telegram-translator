import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


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
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        return cls(
            telegram_token=token,
            allowed_chat_id=int(os.getenv("ALLOWED_CHAT_ID", "0")),
            drop_pending_updates=os.getenv(
                "DROP_PENDING_UPDATES", "true"
            ).lower() in {"1", "true", "yes"},
            db_path=os.getenv("DB_PATH", "translator.db"),
        )
