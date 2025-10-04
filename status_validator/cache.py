from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


def compute_comment_hash(comment: Optional[str]) -> str:
    """Return a stable hash for the provided comment text."""

    normalized = (comment or "").strip()
    return sha256(normalized.encode("utf-8")).hexdigest()


class CacheStore:
    """Persist and retrieve cached LLM payloads for sheet rows."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()  # Thread-safe access to SQLite
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False allows connection use from multiple threads
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_cache (
                    source_id TEXT NOT NULL,
                    sheet_name TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    status_text TEXT NOT NULL,
                    comment_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (source_id, sheet_name, row_number)
                )
                """
            )

    def close(self) -> None:
        with self._lock:  # Thread-safe close
            self._conn.close()

    def get_payload(
        self,
        *,
        source_id: str,
        sheet_name: str,
        row_number: int,
        status_text: str,
        comment_hash: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch cached payload if status and comment hash match."""

        with self._lock:  # Thread-safe database access
            cursor = self._conn.execute(
                """
                SELECT payload_json
                FROM llm_cache
                WHERE source_id = ?
                  AND sheet_name = ?
                  AND row_number = ?
                  AND status_text = ?
                  AND comment_hash = ?
                """,
                (source_id, sheet_name, row_number, status_text, comment_hash),
            )
            row = cursor.fetchone()
        
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:  # pragma: no cover - defensive logging path
            LOGGER.warning(
                "Cached payload for row %s is not valid JSON; ignoring",
                row_number,
            )
            return None

    def store_payload(
        self,
        *,
        source_id: str,
        sheet_name: str,
        row_number: int,
        status_text: str,
        comment_hash: str,
        payload: Dict[str, Any],
    ) -> None:
        """Persist payload for the given row, overriding previous entry."""

        payload_json = json.dumps(payload, ensure_ascii=False)
        timestamp = time.time()
        
        with self._lock:  # Thread-safe database access
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO llm_cache (
                        source_id,
                        sheet_name,
                        row_number,
                        status_text,
                        comment_hash,
                        payload_json,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, sheet_name, row_number)
                    DO UPDATE SET
                        status_text = excluded.status_text,
                        comment_hash = excluded.comment_hash,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_id,
                        sheet_name,
                        row_number,
                        status_text,
                        comment_hash,
                        payload_json,
                        timestamp,
                    ),
                )
