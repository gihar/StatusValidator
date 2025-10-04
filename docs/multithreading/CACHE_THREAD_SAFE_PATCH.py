"""
Патч для status_validator/cache.py для обеспечения thread-safety.

Этот файл показывает необходимые изменения для безопасной
работы с SQLite кешем в многопоточном режиме.

ПРИМЕНЕНИЕ:
1. Добавить import threading в начало cache.py
2. Добавить self._lock в __init__
3. Обернуть операции с базой данных в with self._lock

Альтернативный подход: использовать connection pool с отдельным
соединением для каждого потока.
"""

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


class CacheStoreThreadSafe:
    """
    Thread-safe версия CacheStore.
    
    Использует threading.Lock для синхронизации доступа к SQLite.
    Подходит для использования с ThreadPoolExecutor.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()  # НОВОЕ: блокировка для синхронизации
        
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        
        # check_same_thread=False позволяет использовать соединение из разных потоков
        # Но мы всё равно защищаем доступ через Lock
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        # Эта операция выполняется только при инициализации, но защитим на всякий случай
        with self._lock:
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
        with self._lock:
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
        # ИЗМЕНЕНО: оборачиваем в lock
        with self._lock:
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
        except json.JSONDecodeError:
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
        
        # ИЗМЕНЕНО: оборачиваем в lock
        with self._lock:
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


class CacheStoreConnectionPool:
    """
    Альтернативная реализация с connection pool.
    
    Каждый поток получает своё собственное соединение к SQLite.
    Это может быть быстрее, чем использование Lock, но требует
    больше ресурсов.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()  # Thread-local storage для соединений
        
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        
        # Создаём схему через временное соединение
        temp_conn = sqlite3.connect(str(path))
        temp_conn.execute(
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
        temp_conn.commit()
        temp_conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Получить соединение для текущего потока."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(str(self._path))
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def close(self) -> None:
        """Закрыть соединение текущего потока."""
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            delattr(self._local, "conn")

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
        conn = self._get_connection()
        cursor = conn.execute(
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
        except json.JSONDecodeError:
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
        
        conn = self._get_connection()
        with conn:
            conn.execute(
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


# РЕКОМЕНДАЦИЯ:
# Использовать CacheStoreThreadSafe с Lock - более простая и надёжная реализация.
# CacheStoreConnectionPool может быть полезна при очень высокой конкуренции
# за доступ к кешу (>10 потоков), но в нашем случае 5 потоков - Lock достаточно.

