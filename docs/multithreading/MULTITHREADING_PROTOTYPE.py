"""
Прототип реализации многопоточности для запросов к LLM
Этот файл демонстрирует, как можно внедрить ThreadPoolExecutor в pipeline.py
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

from status_validator.config import AppConfig
from status_validator.google_sheets import GoogleSheetsClient
from status_validator.llm_client import LLMClient
from status_validator.models import StatusEntry, ValidationResult
from status_validator.pipeline import validate_entry

LOGGER = logging.getLogger(__name__)


def validate_entry_with_retry(
    entry: StatusEntry,
    config: AppConfig,
    sheets_client: GoogleSheetsClient,
    llm_client: LLMClient,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Tuple[StatusEntry, ValidationResult | None, Exception | None]:
    """
    Валидация записи с retry logic для обработки rate limiting.
    
    Args:
        entry: Запись для валидации
        config: Конфигурация приложения
        sheets_client: Клиент Google Sheets
        llm_client: Клиент LLM
        max_retries: Максимальное количество попыток при rate limiting
        base_delay: Базовая задержка для exponential backoff (секунды)
    
    Returns:
        Кортеж (entry, result, error)
        - result будет None если произошла ошибка
        - error будет None если валидация успешна
    """
    for attempt in range(max_retries):
        try:
            result = validate_entry(entry, config, sheets_client, llm_client)
            return (entry, result, None)
        except Exception as exc:
            error_message = str(exc).lower()
            
            # Проверяем, является ли ошибка rate limiting
            is_rate_limit = any(
                indicator in error_message
                for indicator in ["rate limit", "429", "too many requests"]
            )
            
            if is_rate_limit and attempt < max_retries - 1:
                # Exponential backoff: 1s, 2s, 4s, ...
                delay = base_delay * (2 ** attempt)
                LOGGER.warning(
                    "Rate limit hit for row %s, retrying in %.1f seconds (attempt %d/%d)",
                    entry.row_number,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
                continue
            
            # Если не rate limit или исчерпаны попытки - возвращаем ошибку
            LOGGER.exception("Validation failed for row %s", entry.row_number)
            return (entry, None, exc)
    
    # Этот код не должен достигаться, но для полноты
    return (entry, None, RuntimeError(f"Failed after {max_retries} attempts"))


def validate_batch_parallel(
    entries: List[StatusEntry],
    config: AppConfig,
    sheets_client: GoogleSheetsClient,
    llm_client: LLMClient,
    max_workers: int = 5,
) -> Tuple[List[Tuple[StatusEntry, ValidationResult]], List[StatusEntry]]:
    """
    Параллельная валидация батча записей.
    
    Args:
        entries: Список записей для валидации
        config: Конфигурация приложения
        sheets_client: Клиент Google Sheets
        llm_client: Клиент LLM
        max_workers: Количество параллельных потоков
    
    Returns:
        Кортеж из двух списков:
        - Успешные результаты: [(entry, result), ...]
        - Записи с ошибками: [entry, ...]
    """
    if not entries:
        return ([], [])
    
    LOGGER.info(
        "Validating %d rows in parallel with %d workers",
        len(entries),
        max_workers,
    )
    
    start_time = time.time()
    successful_results = []
    failed_entries = []
    
    # Используем ThreadPoolExecutor для параллельной обработки
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Отправляем все задачи в executor
        future_to_entry = {
            executor.submit(
                validate_entry_with_retry,
                entry,
                config,
                sheets_client,
                llm_client,
            ): entry
            for entry in entries
        }
        
        # Обрабатываем результаты по мере готовности
        completed = 0
        for future in as_completed(future_to_entry):
            completed += 1
            entry = future_to_entry[future]
            
            try:
                entry, result, error = future.result()
                
                if result is not None:
                    successful_results.append((entry, result))
                    LOGGER.debug(
                        "✓ Row %s validated successfully (%d/%d)",
                        entry.row_number,
                        completed,
                        len(entries),
                    )
                else:
                    failed_entries.append(entry)
                    LOGGER.warning(
                        "✗ Row %s validation failed: %s (%d/%d)",
                        entry.row_number,
                        error,
                        completed,
                        len(entries),
                    )
            except Exception as exc:
                # Защита на случай неожиданных ошибок
                LOGGER.exception(
                    "Unexpected error processing row %s",
                    entry.row_number,
                )
                failed_entries.append(entry)
    
    elapsed_time = time.time() - start_time
    avg_time_per_entry = elapsed_time / len(entries) if entries else 0
    
    LOGGER.info(
        "Batch validation complete: %d successful, %d failed, %.2fs total (%.2fs per entry)",
        len(successful_results),
        len(failed_entries),
        elapsed_time,
        avg_time_per_entry,
    )
    
    # Сортируем результаты по порядку row_number для детерминированного вывода
    successful_results.sort(key=lambda x: x[0].row_number)
    
    return (successful_results, failed_entries)


def validate_batch_parallel_ordered(
    entries: List[StatusEntry],
    config: AppConfig,
    sheets_client: GoogleSheetsClient,
    llm_client: LLMClient,
    max_workers: int = 5,
) -> Tuple[List[Tuple[StatusEntry, ValidationResult]], List[StatusEntry]]:
    """
    Параллельная валидация с сохранением порядка записей.
    
    В отличие от validate_batch_parallel, эта функция гарантирует,
    что результаты будут в том же порядке, что и входные записи.
    Это удобнее для некоторых сценариев использования.
    
    Args:
        entries: Список записей для валидации
        config: Конфигурация приложения
        sheets_client: Клиент Google Sheets
        llm_client: Клиент LLM
        max_workers: Количество параллельных потоков
    
    Returns:
        Кортеж из двух списков:
        - Успешные результаты: [(entry, result), ...] в исходном порядке
        - Записи с ошибками: [entry, ...]
    """
    if not entries:
        return ([], [])
    
    LOGGER.info(
        "Validating %d rows in parallel (ordered) with %d workers",
        len(entries),
        max_workers,
    )
    
    start_time = time.time()
    
    # Используем ThreadPoolExecutor.map для сохранения порядка
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Создаем задачи с дополнительными аргументами через lambda
        results = executor.map(
            lambda entry: validate_entry_with_retry(
                entry, config, sheets_client, llm_client
            ),
            entries,
        )
    
    # Разделяем успешные и неуспешные результаты
    successful_results = []
    failed_entries = []
    
    for entry, result, error in results:
        if result is not None:
            successful_results.append((entry, result))
        else:
            failed_entries.append(entry)
    
    elapsed_time = time.time() - start_time
    avg_time_per_entry = elapsed_time / len(entries) if entries else 0
    
    LOGGER.info(
        "Batch validation complete: %d successful, %d failed, %.2fs total (%.2fs per entry)",
        len(successful_results),
        len(failed_entries),
        elapsed_time,
        avg_time_per_entry,
    )
    
    return (successful_results, failed_entries)


# Пример использования:
#
# В main.py можно заменить:
#
#   for entry in batch:
#       result = validate_entry(entry, config, sheets_client, llm_client)
#       results.append(result)
#
# На:
#
#   max_workers = config.llm.get("max_workers", 5)
#   successful, failed = validate_batch_parallel(
#       batch, config, sheets_client, llm_client, max_workers
#   )
#   for entry, result in successful:
#       results.append(result)
#   failed_entries.extend(failed)

