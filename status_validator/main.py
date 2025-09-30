from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv
from .cache import CacheStore, compute_comment_hash
from .config import load_config
from .google_sheets import GoogleSheetsClient
from .llm_client import LLMClient
from .pipeline import build_entries, build_result_from_payload, results_to_rows, validate_entry

def _load_env_files(config_path: Path) -> None:
    """Load environment variables from .env files."""

    # Load default .env in current working directory if present
    load_dotenv(override=False)

    # Load .env placed next to the config file if it exists
    config_env = config_path.parent / ".env"
    if config_env.exists():
        load_dotenv(dotenv_path=config_env, override=False)


LOGGER = logging.getLogger("status_validator")


def _normalize_identifier(value: str | None) -> str:
    return (value or "").strip().casefold()


def _extract_row_number(cell_reference: str) -> int | None:
    digits = [ch for ch in cell_reference if ch.isdigit()]
    if not digits:
        return None
    return int("".join(digits))


def _parse_updated_range(range_str: str) -> tuple[int, int] | None:
    if not range_str:
        return None
    _, _, range_body = range_str.partition("!")
    if not range_body:
        range_body = range_str
    start_ref, sep, end_ref = range_body.partition(":")
    if not sep:
        end_ref = start_ref
    start_row = _extract_row_number(start_ref)
    end_row = _extract_row_number(end_ref)
    if start_row is None or end_row is None:
        return None
    return start_row, end_row


def _parse_check_date_value(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    LOGGER.debug("Unable to parse check date value '%s'", text)
    return None


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate project status updates with an LLM")
    parser.add_argument("--config", required=True, help="Path to the YAML configuration file")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for the number of rows to process",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write results to Google Sheets; print them to stdout instead",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force revalidation even when cached results exist",
    )
    parser.add_argument(
        "--checkdate",
        action="store_true",
        help=(
            "Force revalidation for a single row when its existing 'Check date' is from a prior week"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = Path(args.config).expanduser().resolve()
    _load_env_files(config_path)
    config = load_config(config_path)
    cache_path = config.cache_path or (config_path.parent / "status_validator_cache.sqlite")
    sheets_client = GoogleSheetsClient(config.sheets)
    LOGGER.info("Fetching source rows from Google Sheets...")
    raw_values = sheets_client.fetch_values()
    entries = build_entries(raw_values, config.columns, config.header_row, config.data_start_row)
    if args.limit is not None:
        entries = entries[: args.limit]

    if not entries:
        LOGGER.warning("No data rows found in the source sheet")
        return 0

    llm_client = LLMClient(config.llm)
    cache_store = CacheStore(cache_path)

    results = []
    processed_entries = []
    processed_check_dates: list[str | None] = []
    processed_model_names: list[str | None] = []
    failed_entries = []
    skipped_missing_identifier = []
    write_entries = []
    write_results = []
    write_check_dates = []
    write_model_names: list[str | None] = []
    last_written_total_count = 0
    last_written_write_count = 0
    identifier_column_present = bool(config.columns.identifier) and any(
        config.columns.identifier in entry.source_values for entry in entries
    )
    project_manager_column_present = bool(config.columns.project_manager) and any(
        config.columns.project_manager in entry.source_values for entry in entries
    )
    check_date = datetime.now().strftime("%d.%m.%Y %H:%M")
    header_rows = results_to_rows(
        [],
        [],
        config.columns,
        include_header=True,
        identifier_column_present=identifier_column_present,
        project_manager_column_present=project_manager_column_present,
        check_dates=[],
        model_names=[],
    )
    expected_header_row = header_rows[0]
    check_date_column_index = expected_header_row.index("Check date")
    model_column_index = expected_header_row.index("Model")
    use_identifier_updates = not args.dry_run and identifier_column_present
    existing_rows_by_project: dict[str, int] = {}
    existing_check_date_by_row_number: dict[int, str | None] = {}
    existing_check_date_by_identifier: dict[str, str | None] = {}
    existing_model_by_row_number: dict[int, str | None] = {}
    existing_model_by_identifier: dict[str, str | None] = {}
    next_available_row = 2
    project_column_index = None
    existing_row_check_dates: list[str | None] = []
    existing_row_model_values: list[str | None] = []
    target_values: list[list[str]] = []
    fetch_target_values = not args.dry_run or args.checkdate
    if fetch_target_values:
        target_values = sheets_client.fetch_target_values()

    current_header = target_values[0] if target_values else []
    if not args.dry_run:
        if current_header != expected_header_row:
            sheets_client.update_target_header(expected_header_row)
            current_header = expected_header_row
        if not target_values:
            target_values = [current_header]
        else:
            target_values[0] = current_header

    if not args.dry_run:
        existing_row_check_dates = [
            row[check_date_column_index] if check_date_column_index < len(row) else ""
            for row in target_values[1:]
        ]
        existing_row_model_values = [
            row[model_column_index] if model_column_index < len(row) else ""
            for row in target_values[1:]
        ]
        if use_identifier_updates:
            try:
                project_column_index = expected_header_row.index("Project name")
            except ValueError:
                use_identifier_updates = False
            else:
                for row_number, row in enumerate(target_values[1:], start=2):
                    if project_column_index < len(row):
                        normalized_key = _normalize_identifier(row[project_column_index])
                        if normalized_key:
                            existing_rows_by_project.setdefault(normalized_key, row_number)
                next_available_row = len(target_values) + 1
        else:
            next_available_row = len(target_values) + 1

    if target_values:
        header_for_lookup = target_values[0]
        try:
            lookup_row_number_idx = header_for_lookup.index("Row Number")
        except ValueError:
            lookup_row_number_idx = None
        try:
            lookup_check_date_idx = header_for_lookup.index("Check date")
        except ValueError:
            lookup_check_date_idx = None
        try:
            lookup_identifier_idx = header_for_lookup.index("Project name")
        except ValueError:
            lookup_identifier_idx = None
        try:
            lookup_model_idx = header_for_lookup.index("Model")
        except ValueError:
            lookup_model_idx = None

        if lookup_row_number_idx is not None and lookup_check_date_idx is not None:
            for row in target_values[1:]:
                row_number_value = (
                    row[lookup_row_number_idx]
                    if lookup_row_number_idx < len(row)
                    else ""
                )
                check_date_value = (
                    row[lookup_check_date_idx]
                    if lookup_check_date_idx < len(row)
                    else ""
                )
                model_value = (
                    row[lookup_model_idx]
                    if lookup_model_idx is not None and lookup_model_idx < len(row)
                    else ""
                )
                try:
                    source_row_number = int(str(row_number_value).strip())
                except (TypeError, ValueError):
                    source_row_number = None
                if source_row_number is not None:
                    existing_check_date_by_row_number[source_row_number] = (
                        check_date_value or None
                    )
                    existing_model_by_row_number[source_row_number] = model_value or None
                if lookup_identifier_idx is not None and lookup_identifier_idx < len(row):
                    identifier_value = row[lookup_identifier_idx]
                    normalized_identifier = _normalize_identifier(identifier_value)
                    if normalized_identifier:
                        existing_check_date_by_identifier[normalized_identifier] = (
                            check_date_value or None
                        )
                        existing_model_by_identifier[normalized_identifier] = (
                            model_value or None
                        )

    checkdate_force_remaining = 1 if args.checkdate else 0
    current_week_start = None
    if args.checkdate:
        now = datetime.now()
        current_week_start = (
            now - timedelta(days=now.weekday())
        ).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        for start in range(0, len(entries), config.batch_size):
            batch = entries[start : start + config.batch_size]
            LOGGER.info("Validating rows %s-%s", batch[0].row_number, batch[-1].row_number)
            for entry in batch:
                LOGGER.debug("Validating row %s", entry.row_number)
                normalized_identifier = _normalize_identifier(entry.identifier)
                has_identifier_column = bool(config.columns.identifier) and (
                    config.columns.identifier in entry.source_values
                )
                if has_identifier_column and not normalized_identifier:
                    LOGGER.warning(
                        "Row %s has no project name; skipping", entry.row_number
                    )
                    skipped_missing_identifier.append(entry)
                    continue

                should_force_current = False
                existing_check_date_value: str | None = None
                if args.checkdate:
                    if normalized_identifier:
                        existing_check_date_value = existing_check_date_by_identifier.get(
                            normalized_identifier
                        )
                    if existing_check_date_value is None:
                        existing_check_date_value = existing_check_date_by_row_number.get(
                            entry.row_number
                        )

                    force_reason: str | None = None
                    force_without_limit = False
                    if not (existing_check_date_value or "").strip():
                        force_reason = "missing Check date"
                        force_without_limit = True
                    else:
                        parsed_check_date = _parse_check_date_value(existing_check_date_value)
                        if parsed_check_date is None:
                            force_reason = (
                                f"unrecognized Check date '{existing_check_date_value}'"
                            )
                        elif (
                            current_week_start is not None
                            and parsed_check_date < current_week_start
                        ):
                            force_reason = f"stale Check date '{existing_check_date_value}'"

                    if force_reason is not None:
                        if force_without_limit or checkdate_force_remaining > 0:
                            should_force_current = True
                            if not force_without_limit:
                                checkdate_force_remaining -= 1
                            LOGGER.info(
                                "Forcing revalidation for row %s due to %s",
                                entry.row_number,
                                force_reason,
                            )

                comment_hash = compute_comment_hash(entry.comment_text)
                cached_payload = None
                if not args.force and not should_force_current:
                    cached_payload = cache_store.get_payload(
                        source_id=config.sheets.source_spreadsheet_id,
                        sheet_name=config.sheets.source_sheet_name,
                        row_number=entry.row_number,
                        status_text=entry.status_text,
                        comment_hash=comment_hash,
                    )
                if cached_payload is not None:
                    LOGGER.debug(
                        "Using cached validation for row %s", entry.row_number
                    )
                    result = build_result_from_payload(
                        entry,
                        cached_payload,
                        config,
                        sheets_client,
                    )
                else:
                    try:
                        result = validate_entry(entry, config, sheets_client, llm_client)
                    except Exception:  # pragma: no cover - defensive logging path
                        LOGGER.exception("Validation failed for row %s; skipping", entry.row_number)
                        failed_entries.append(entry)
                        continue
                    cache_store.store_payload(
                        source_id=config.sheets.source_spreadsheet_id,
                        sheet_name=config.sheets.source_sheet_name,
                        row_number=entry.row_number,
                        status_text=entry.status_text,
                        comment_hash=comment_hash,
                        payload=result.raw_response,
                    )
                if cached_payload is not None:
                    entry_model_value = (
                        existing_model_by_identifier.get(normalized_identifier)
                        if normalized_identifier
                        else None
                    )
                    if entry_model_value is None:
                        entry_model_value = existing_model_by_row_number.get(
                            entry.row_number
                        )
                else:
                    entry_model_value = llm_client.model_name

                entry_check_date = None if cached_payload is not None else check_date
                processed_entries.append(entry)
                processed_check_dates.append(entry_check_date)
                processed_model_names.append(entry_model_value)
                results.append(result)
                if (
                    entry_check_date is not None
                    and use_identifier_updates
                    and not args.dry_run
                ):
                    write_entries.append(entry)
                    write_results.append(result)
                    write_check_dates.append(entry_check_date)
                    write_model_names.append(entry_model_value)

            if not args.dry_run:
                if use_identifier_updates and len(write_results) > last_written_write_count:
                    new_entries = write_entries[last_written_write_count:]
                    new_results = write_results[last_written_write_count:]
                    new_check_dates = write_check_dates[last_written_write_count:]
                    new_model_names = write_model_names[last_written_write_count:]
                    output_rows = results_to_rows(
                        new_entries,
                        new_results,
                        config.columns,
                        include_header=False,
                        identifier_column_present=identifier_column_present,
                        project_manager_column_present=project_manager_column_present,
                        check_dates=new_check_dates,
                        model_names=new_model_names,
                    )
                    row_updates: dict[int, list[str]] = {}
                    updated_keys: dict[int, str] = {}
                    rows_to_append: list[tuple[str | None, list[str]]] = []
                    for entry, row_values in zip(new_entries, output_rows):
                        normalized_key = _normalize_identifier(entry.identifier)
                        target_row = (
                            existing_rows_by_project.get(normalized_key)
                            if normalized_key
                            else None
                        )
                        if normalized_key and target_row is not None:
                            row_updates[target_row] = row_values
                            updated_keys[target_row] = normalized_key
                        else:
                            rows_to_append.append((normalized_key, row_values))

                    if row_updates:
                        sheets_client.update_target_rows(row_updates)
                        LOGGER.info(
                            "Updated %s existing rows in target sheet (progress)",
                            len(row_updates),
                        )
                        for row_number, normalized_key in updated_keys.items():
                            row_values = row_updates[row_number]
                            existing_rows_by_project[normalized_key] = row_number
                            list_index = row_number - 2
                            if list_index >= 0:
                                check_value = (
                                    row_values[check_date_column_index]
                                    if check_date_column_index < len(row_values)
                                    else ""
                                )
                                model_value = (
                                    row_values[model_column_index]
                                    if model_column_index < len(row_values)
                                    else ""
                                )
                                if list_index < len(existing_row_check_dates):
                                    existing_row_check_dates[list_index] = check_value
                                else:
                                    existing_row_check_dates.extend(
                                        [""] * (list_index - len(existing_row_check_dates) + 1)
                                    )
                                    existing_row_check_dates[list_index] = check_value
                                if list_index < len(existing_row_model_values):
                                    existing_row_model_values[list_index] = model_value
                                else:
                                    existing_row_model_values.extend(
                                        [""] * (list_index - len(existing_row_model_values) + 1)
                                    )
                                    existing_row_model_values[list_index] = model_value
                            source_row_number_value = row_values[0] if row_values else ""
                            try:
                                source_row_number = int(str(source_row_number_value).strip())
                            except (TypeError, ValueError):
                                source_row_number = None
                            if source_row_number is not None:
                                existing_check_date_by_row_number[source_row_number] = (
                                    check_value or None
                                )
                                existing_model_by_row_number[source_row_number] = (
                                    model_value or None
                                )
                            if normalized_key:
                                existing_check_date_by_identifier[normalized_key] = (
                                    check_value or None
                                )
                                existing_model_by_identifier[normalized_key] = (
                                    model_value or None
                                )

                    if rows_to_append:
                        append_payload = [values for _, values in rows_to_append]
                        response = sheets_client.append_results(append_payload) or {}
                        updates_block = (
                            response.get("updates")
                            if isinstance(response, dict)
                            else None
                        )
                        updated_range = (
                            updates_block.get("updatedRange")
                            if isinstance(updates_block, dict)
                            else ""
                        )
                        row_range = _parse_updated_range(updated_range)
                        if row_range is None:
                            start_row = next_available_row
                            end_row = start_row + len(rows_to_append) - 1
                        else:
                            start_row, end_row = row_range
                        LOGGER.info(
                            "Appended %s new result rows to target sheet (progress)",
                            len(rows_to_append),
                        )
                        current_row = start_row
                        for normalized_key, row_values in rows_to_append:
                            if normalized_key:
                                existing_rows_by_project[normalized_key] = current_row
                            list_index = current_row - 2
                            if list_index >= 0:
                                check_value = (
                                    row_values[check_date_column_index]
                                    if check_date_column_index < len(row_values)
                                    else ""
                                )
                                model_value = (
                                    row_values[model_column_index]
                                    if model_column_index < len(row_values)
                                    else ""
                                )
                                if list_index < len(existing_row_check_dates):
                                    existing_row_check_dates[list_index] = check_value
                                else:
                                    existing_row_check_dates.extend(
                                        [""] * (list_index - len(existing_row_check_dates) + 1)
                                    )
                                    existing_row_check_dates[list_index] = check_value
                                if list_index < len(existing_row_model_values):
                                    existing_row_model_values[list_index] = model_value
                                else:
                                    existing_row_model_values.extend(
                                        [""] * (list_index - len(existing_row_model_values) + 1)
                                    )
                                    existing_row_model_values[list_index] = model_value
                                source_row_number_value = row_values[0] if row_values else ""
                                try:
                                    source_row_number = int(str(source_row_number_value).strip())
                                except (TypeError, ValueError):
                                    source_row_number = None
                                if source_row_number is not None:
                                    existing_check_date_by_row_number[source_row_number] = (
                                        check_value or None
                                    )
                                    existing_model_by_row_number[source_row_number] = (
                                        model_value or None
                                    )
                                if normalized_key:
                                    existing_check_date_by_identifier[normalized_key] = (
                                        check_value or None
                                    )
                                    existing_model_by_identifier[normalized_key] = (
                                        model_value or None
                                    )
                            current_row += 1
                        next_available_row = max(next_available_row, end_row + 1)
                    last_written_write_count = len(write_results)
                elif not use_identifier_updates and len(results) > last_written_total_count:
                    new_entries = processed_entries[last_written_total_count:]
                    new_results = results[last_written_total_count:]
                    new_check_dates = processed_check_dates[last_written_total_count:]
                    new_model_names = processed_model_names[last_written_total_count:]
                    new_results_count = len(new_results)
                    include_header = last_written_total_count == 0
                    check_dates_for_rows: list[str | None] = []
                    model_names_for_rows: list[str | None] = []
                    for idx, date_value in enumerate(new_check_dates):
                        if date_value is not None:
                            check_dates_for_rows.append(date_value)
                        else:
                            seq_index = last_written_total_count + idx
                            existing_value = (
                                existing_row_check_dates[seq_index]
                                if seq_index < len(existing_row_check_dates)
                                else None
                            )
                            check_dates_for_rows.append(existing_value)
                    for idx, model_value in enumerate(new_model_names):
                        if model_value is not None:
                            model_names_for_rows.append(model_value)
                        else:
                            seq_index = last_written_total_count + idx
                            existing_value = (
                                existing_row_model_values[seq_index]
                                if seq_index < len(existing_row_model_values)
                                else None
                            )
                            model_names_for_rows.append(existing_value)
                    output_rows = results_to_rows(
                        new_entries,
                        new_results,
                        config.columns,
                        include_header=include_header,
                        identifier_column_present=identifier_column_present,
                        project_manager_column_present=project_manager_column_present,
                        check_dates=check_dates_for_rows,
                        model_names=model_names_for_rows,
                    )
                    if include_header:
                        LOGGER.info(
                            "Writing %s result rows to target sheet (progress)",
                            new_results_count,
                        )
                        sheets_client.overwrite_results(output_rows)
                    else:
                        LOGGER.info(
                            "Appending %s result rows to target sheet (progress)",
                            new_results_count,
                        )
                        sheets_client.append_results(output_rows)
                    for idx, value in enumerate(check_dates_for_rows):
                        seq_index = last_written_total_count + idx
                        stored_value = value or ""
                        if seq_index < len(existing_row_check_dates):
                            existing_row_check_dates[seq_index] = stored_value
                        else:
                            existing_row_check_dates.append(stored_value)
                    for idx, value in enumerate(model_names_for_rows):
                        seq_index = last_written_total_count + idx
                        stored_value = value or ""
                        if seq_index < len(existing_row_model_values):
                            existing_row_model_values[seq_index] = stored_value
                        else:
                            existing_row_model_values.append(stored_value)
                    last_written_total_count = len(results)

        if skipped_missing_identifier:
            LOGGER.warning(
                "Skipped %s rows without project name",
                len(skipped_missing_identifier),
            )

        if failed_entries:
            LOGGER.warning("Skipped %s rows due to errors", len(failed_entries))

        if args.dry_run:
            output_rows = results_to_rows(
                processed_entries,
                results,
                config.columns,
                identifier_column_present=identifier_column_present,
                project_manager_column_present=project_manager_column_present,
                check_dates=processed_check_dates,
                model_names=processed_model_names,
            )
            LOGGER.info("Dry run enabled; writing results to stdout")
            print(json.dumps(output_rows, ensure_ascii=False, indent=2))
        else:
            if len(results) == 0:
                if use_identifier_updates:
                    LOGGER.info("No rows validated; ensuring target header is up to date")
                    sheets_client.update_target_header(expected_header_row)
                else:
                    LOGGER.info("No rows validated; clearing target sheet")
                    sheets_client.overwrite_results(header_rows)
            if use_identifier_updates:
                LOGGER.info(
                    "Completed writing %s result rows to target sheet",
                    len(write_results),
                )
            else:
                LOGGER.info(
                    "Completed writing %s result rows to target sheet",
                    len(results),
                )

        LOGGER.info("Completed validation for %s rows", len(results))
        return 0
    finally:
        cache_store.close()


def run() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    run()
