import asyncio
import json
from typing import Optional

import aiosqlite

from app.models import MessageLink


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS message_links (
                chat_id             INTEGER NOT NULL,
                source_message_id   INTEGER NOT NULL,
                source_user_id      INTEGER,
                source_text         TEXT NOT NULL,
                bot_message_ids     TEXT NOT NULL DEFAULT '[]',
                created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, source_message_id)
            )
            """
        )
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def claim_message(
        self, chat_id: int, message_id: int, user_id: Optional[int], text: str
    ) -> bool:
        """Возвращает True только для первого обработчика сообщения."""
        assert self.db is not None
        async with self.lock:
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO message_links (
                    chat_id, source_message_id, source_user_id, source_text
                ) VALUES (?, ?, ?, ?)
                """,
                (chat_id, message_id, user_id, text),
            )
            await self.db.commit()
            return cursor.rowcount == 1

    async def get_message(
        self, chat_id: int, message_id: int
    ) -> Optional[MessageLink]:
        assert self.db is not None
        cursor = await self.db.execute(
            """
            SELECT source_text, bot_message_ids FROM message_links
            WHERE chat_id = ? AND source_message_id = ?
            """,
            (chat_id, message_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return MessageLink(source_text=row[0], bot_message_ids=json.loads(row[1]))

    async def update_source_text(
        self, chat_id: int, message_id: int, text: str
    ) -> tuple[Optional[MessageLink], bool]:
        """Обновляет текст оригинала и сообщает, действительно ли он изменился."""
        assert self.db is not None
        async with self.lock:
            cursor = await self.db.execute(
                """
                SELECT source_text, bot_message_ids FROM message_links
                WHERE chat_id = ? AND source_message_id = ?
                """,
                (chat_id, message_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None, False
            old_text = row[0]
            bot_message_ids = json.loads(row[1])
            if old_text == text:
                return MessageLink(old_text, bot_message_ids), False
            await self.db.execute(
                """
                UPDATE message_links
                SET source_text = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND source_message_id = ?
                """,
                (text, chat_id, message_id),
            )
            await self.db.commit()
            return MessageLink(text, bot_message_ids), True

    async def set_bot_messages(
        self, chat_id: int, source_message_id: int, bot_message_ids: list[int]
    ) -> None:
        assert self.db is not None
        async with self.lock:
            await self.db.execute(
                """
                UPDATE message_links
                SET bot_message_ids = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND source_message_id = ?
                """,
                (json.dumps(bot_message_ids), chat_id, source_message_id),
            )
            await self.db.commit()
