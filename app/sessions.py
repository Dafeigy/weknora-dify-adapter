"""Persistent mapping between Dify conversations and WeKnora sessions."""

import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4


class ConversationStore:
    """Store opaque Dify conversation IDs without retaining raw Dify user IDs."""

    def __init__(self, database_path: str) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    dify_conversation_id TEXT PRIMARY KEY,
                    user_hash TEXT NOT NULL,
                    weknora_session_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS conversations_user_hash "
                "ON conversations(user_hash)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _user_hash(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def create(self, user_id: str, weknora_session_id: str) -> str:
        conversation_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(dify_conversation_id, user_hash, weknora_session_id) VALUES (?, ?, ?)",
                (conversation_id, self._user_hash(user_id), weknora_session_id),
            )
        return conversation_id

    def get(self, user_id: str, conversation_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT weknora_session_id FROM conversations "
                "WHERE dify_conversation_id = ? AND user_hash = ?",
                (conversation_id, self._user_hash(user_id)),
            ).fetchone()
        return str(row["weknora_session_id"]) if row else None
