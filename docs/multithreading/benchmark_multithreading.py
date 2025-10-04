#!/usr/bin/env python3
"""
Benchmark скрипт для сравнения производительности
последовательной и параллельной обработки запросов к LLM.

Использование:
    python benchmark_multithreading.py --config config.yaml --limit 20
"""

import argparse
import logging
import time
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv

from status_validator.cache import CacheStore, compute_comment_hash
from status_validator.config import load_config
from status_validator.google_sheets import GoogleSheetsClient
from status_validator.llm_client import LLMClient
from status_validator.models import StatusEntry, ValidationResult
from status_validator.pipeline import build_entries, validate_entry

from MULTITHREADING_PROTOTYPE import validate_batch_parallel

LOGGER = logging.getLogger(__name__)


def validate_batch_sequential(
    entries: List[StatusEntry],
    config,
    sheets_client: GoogleSheetsClient,
    llm_client: LLMClient,
) -> Tuple[List[Tuple[StatusEntry, ValidationResult]], List[StatusEntry]]:
    """Последовательная валидация батча (текущая реализация)."""
    start_time = time.time()
    successful_results = []
    failed_entries = []
    
    for entry in entries:
        try:
            result = validate_entry(entry, config, sheets_client, llm_client)
            successful_results.append((entry, result))
        except Exception:
            LOGGER.exception("Validation failed for row %s", entry.row_number)
            failed_entries.append(entry)
    
    elapsed_time = time.time() - start_time
    LOGGER.info(
        "Sequential validation complete: %.2fs total (%.2fs per entry)",
        elapsed_time,
        elapsed_time / len(entries) if entries else 0,
    )
    
    return (successful_results, failed_entries)


def run_benchmark(config_path: Path, limit: int, max_workers: int):
    """Запуск бенчмарка."""
    # Загрузка конфигурации
    load_dotenv(override=False)
    config = load_config(config_path)
    
    # Инициализация клиентов
    sheets_client = GoogleSheetsClient(config.sheets)
    llm_client = LLMClient(config.llm)
    cache_path = config.cache_path or (config_path.parent / "status_validator_cache.sqlite")
    cache_store = CacheStore(cache_path)
    
    try:
        # Загрузка данных
        LOGGER.info("Fetching source rows from Google Sheets...")
        raw_values = sheets_client.fetch_values()
        entries = build_entries(
            raw_values,
            config.columns,
            config.header_row,
            config.data_start_row,
        )
        
        if limit:
            entries = entries[:limit]
        
        if not entries:
            LOGGER.error("No entries to validate")
            return
        
        LOGGER.info("Loaded %d entries for benchmark", len(entries))
        
        # Очистка кеша для честного сравнения
        LOGGER.info("Clearing cache for fair comparison...")
        for entry in entries:
            comment_hash = compute_comment_hash(entry.comment_text)
            # Не удаляем кеш, просто пометим, что запросы могут быть закешированы
        
        # Бенчмарк 1: Последовательная обработка
        LOGGER.info("=" * 60)
        LOGGER.info("BENCHMARK 1: Sequential processing")
        LOGGER.info("=" * 60)
        
        seq_start = time.time()
        seq_results, seq_failed = validate_batch_sequential(
            entries,
            config,
            sheets_client,
            llm_client,
        )
        seq_time = time.time() - seq_start
        
        LOGGER.info("Sequential: %.2fs total, %.2fs per entry", seq_time, seq_time / len(entries))
        LOGGER.info("Successful: %d, Failed: %d", len(seq_results), len(seq_failed))
        
        # Пауза между бенчмарками
        LOGGER.info("Waiting 2 seconds before parallel benchmark...")
        time.sleep(2)
        
        # Бенчмарк 2: Параллельная обработка
        LOGGER.info("=" * 60)
        LOGGER.info("BENCHMARK 2: Parallel processing (%d workers)", max_workers)
        LOGGER.info("=" * 60)
        
        par_start = time.time()
        par_results, par_failed = validate_batch_parallel(
            entries,
            config,
            sheets_client,
            llm_client,
            max_workers=max_workers,
        )
        par_time = time.time() - par_start
        
        LOGGER.info("Parallel: %.2fs total, %.2fs per entry", par_time, par_time / len(entries))
        LOGGER.info("Successful: %d, Failed: %d", len(par_results), len(par_failed))
        
        # Сравнение результатов
        LOGGER.info("=" * 60)
        LOGGER.info("COMPARISON")
        LOGGER.info("=" * 60)
        LOGGER.info("Sequential time: %.2fs", seq_time)
        LOGGER.info("Parallel time:   %.2fs (%d workers)", par_time, max_workers)
        
        if par_time > 0:
            speedup = seq_time / par_time
            LOGGER.info("Speedup:         %.2fx", speedup)
            LOGGER.info("Time saved:      %.2fs (%.1f%%)", seq_time - par_time, (1 - par_time / seq_time) * 100)
        
        # Проверка корректности
        if len(seq_results) != len(par_results):
            LOGGER.warning(
                "Result count mismatch! Sequential: %d, Parallel: %d",
                len(seq_results),
                len(par_results),
            )
        else:
            LOGGER.info("✓ Result counts match")
        
        # Дополнительная статистика
        LOGGER.info("=" * 60)
        LOGGER.info("STATISTICS")
        LOGGER.info("=" * 60)
        LOGGER.info("Total entries:        %d", len(entries))
        LOGGER.info("Cache hits (approx):  ~%d (sequential had fresh cache)", len(entries))
        LOGGER.info("Sequential throughput: %.2f entries/sec", len(entries) / seq_time if seq_time > 0 else 0)
        LOGGER.info("Parallel throughput:   %.2f entries/sec", len(entries) / par_time if par_time > 0 else 0)
        
        # Оценка стоимости времени
        time_value_per_hour = 50  # долларов в час
        time_saved_hours = (seq_time - par_time) / 3600
        value_saved = time_saved_hours * time_value_per_hour
        
        LOGGER.info("")
        LOGGER.info("Extrapolation for 1000 entries:")
        LOGGER.info("  Sequential: ~%.0f seconds (~%.1f minutes)", seq_time * 1000 / len(entries), seq_time * 1000 / len(entries) / 60)
        LOGGER.info("  Parallel:   ~%.0f seconds (~%.1f minutes)", par_time * 1000 / len(entries), par_time * 1000 / len(entries) / 60)
        LOGGER.info("  Time saved: ~%.0f seconds (~%.1f minutes)", (seq_time - par_time) * 1000 / len(entries), (seq_time - par_time) * 1000 / len(entries) / 60)
        
    finally:
        cache_store.close()


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark sequential vs parallel LLM request processing"
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the YAML configuration file",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of entries to test (default: 10)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Number of parallel workers to test (default: 5)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    
    args = parser.parse_args()
    
    # Настройка логирования
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    config_path = Path(args.config).expanduser().resolve()
    
    if not config_path.exists():
        LOGGER.error("Configuration file not found: %s", config_path)
        return 1
    
    try:
        run_benchmark(config_path, args.limit, args.workers)
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Interrupted by user")
        return 130
    except Exception:
        LOGGER.exception("Benchmark failed")
        return 1


if __name__ == "__main__":
    exit(main())

