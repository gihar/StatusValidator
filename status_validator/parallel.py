"""Parallel processing module for LLM validation requests using ThreadPoolExecutor."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from .config import AppConfig
from .google_sheets import GoogleSheetsClient
from .llm_client import LLMClient
from .models import StatusEntry, ValidationResult
from .pipeline import validate_entry

LOGGER = logging.getLogger(__name__)


def validate_entry_with_retry(
    entry: StatusEntry,
    config: AppConfig,
    sheets_client: GoogleSheetsClient,
    llm_client: LLMClient,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Tuple[StatusEntry, Optional[ValidationResult], Optional[Exception]]:
    """
    Validate entry with retry logic for handling rate limiting.
    
    Args:
        entry: Entry to validate
        config: Application configuration
        sheets_client: Google Sheets client
        llm_client: LLM client
        max_retries: Maximum number of retry attempts for rate limiting
        base_delay: Base delay for exponential backoff (seconds)
    
    Returns:
        Tuple (entry, result, error)
        - result will be None if error occurred
        - error will be None if validation succeeded
    """
    for attempt in range(max_retries):
        try:
            result = validate_entry(entry, config, sheets_client, llm_client)
            return (entry, result, None)
        except Exception as exc:
            error_message = str(exc).lower()
            
            # Check if this is a rate limiting error
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
            
            # If not rate limit or retries exhausted - return error
            LOGGER.exception("Validation failed for row %s", entry.row_number)
            return (entry, None, exc)
    
    # Should not reach here, but for completeness
    return (entry, None, RuntimeError(f"Failed after {max_retries} attempts"))


def validate_batch_parallel(
    entries: List[StatusEntry],
    config: AppConfig,
    sheets_client: GoogleSheetsClient,
    llm_client: LLMClient,
    max_workers: int = 5,
) -> Tuple[List[Tuple[StatusEntry, ValidationResult]], List[StatusEntry]]:
    """
    Validate batch of entries in parallel using ThreadPoolExecutor.
    
    Args:
        entries: List of entries to validate
        config: Application configuration
        sheets_client: Google Sheets client
        llm_client: LLM client
        max_workers: Number of parallel threads
    
    Returns:
        Tuple of two lists:
        - Successful results: [(entry, result), ...]
        - Failed entries: [entry, ...]
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
    
    # Use ThreadPoolExecutor for parallel processing
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks to executor
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
        
        # Process results as they complete
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
                # Protection against unexpected errors
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
    
    # Sort results by row_number for deterministic output
    successful_results.sort(key=lambda x: x[0].row_number)
    
    return (successful_results, failed_entries)

