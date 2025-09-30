from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Sequence

from .config import AppConfig, ColumnsConfig
from .google_sheets import GoogleSheetsClient
from .llm_client import LLMClient
from .models import StatusEntry, ValidationResult
from .prompt_builder import build_validation_messages

LOGGER = logging.getLogger("status_validator.pipeline")

def _normalize_header(header: str) -> str:
    return header.strip().lower()


def _column_index(header_map: Dict[str, int], column_name: str) -> int:
    key = _normalize_header(column_name)
    if key not in header_map:
        available = ", ".join(sorted(header_map))
        msg = f"Column '{column_name}' not found. Available columns: {available}"
        raise ValueError(msg)
    return header_map[key]


def _make_hyperlink(url: str, text: str) -> str:
    safe_url = url.replace('"', '""')
    safe_text = (text or url).replace('"', '""')
    return f'=HYPERLINK("{safe_url}"; "{safe_text}")'


def build_entries(
    values: List[List[str]],
    columns: ColumnsConfig,
    header_row: int,
    data_start_row: int,
) -> List[StatusEntry]:
    if not values:
        return []

    if header_row < 1:
        raise ValueError("header_row must be 1 or greater")
    if data_start_row <= header_row:
        raise ValueError("data_start_row must be greater than header_row")

    header_idx = header_row - 1
    if header_idx >= len(values):
        return []

    header = values[header_idx]
    header_map = {_normalize_header(name): idx for idx, name in enumerate(header)}

    status_idx = _column_index(header_map, columns.status)
    comment_idx = _column_index(header_map, columns.comment)
    completion_idx = None
    if columns.completion_date:
        try:
            completion_idx = _column_index(header_map, columns.completion_date)
        except ValueError:
            LOGGER.warning(
                "Column '%s' not found in source sheet; skipping",
                columns.completion_date,
            )

    identifier_idx = None
    if columns.identifier:
        try:
            identifier_idx = _column_index(header_map, columns.identifier)
        except ValueError:
            LOGGER.warning(
                "Column '%s' not found in source sheet; skipping",
                columns.identifier,
            )

    project_manager_idx = None
    if columns.project_manager:
        try:
            project_manager_idx = _column_index(header_map, columns.project_manager)
        except ValueError:
            LOGGER.warning(
                "Column '%s' not found in source sheet; skipping",
                columns.project_manager,
            )

    entries: List[StatusEntry] = []
    start_idx = max(data_start_row - 1, header_idx + 1)
    for absolute_idx in range(start_idx, len(values)):
        row = values[absolute_idx]

        def _get(idx: int | None) -> str | None:
            if idx is None:
                return None
            if idx < len(row):
                return row[idx]
            return None

        source_values = {
            header[idx]: row[idx] if idx < len(row) else ""
            for idx in range(len(header))
        }

        status_text = _get(status_idx) or ""
        comment_text = _get(comment_idx) or ""
        completion_value = _get(completion_idx) if completion_idx is not None else None

        identifier_value = None
        if identifier_idx is not None:
            identifier_value = _get(identifier_idx) or ""
            source_values[columns.identifier] = identifier_value or ""

        project_manager_value = None
        if project_manager_idx is not None:
            project_manager_value = _get(project_manager_idx) or ""
            source_values[columns.project_manager] = project_manager_value or ""

        entries.append(
            StatusEntry(
                row_index=len(entries),
                row_number=absolute_idx + 1,
                status_text=status_text,
                comment_text=comment_text,
                completion_date=completion_value,
                identifier=identifier_value,
                project_manager=project_manager_value,
                source_values=source_values,
            )
        )

    return entries


def build_result_from_payload(
    entry: StatusEntry,
    payload: Dict[str, Any],
    config: AppConfig,
    sheets_client: GoogleSheetsClient,
) -> ValidationResult:
    issues_block = payload.get("issues", [])
    if isinstance(issues_block, str):
        issues_items = [issues_block]
    elif isinstance(issues_block, list):
        issues_items = [str(item) for item in issues_block if str(item).strip()]
    else:
        issues_items = [json.dumps(issues_block, ensure_ascii=False)]

    if config.allowed_statuses and entry.status_text not in config.allowed_statuses:
        extra_note = (
            f"Status value '{entry.status_text}' is outside the allowed list: "
            f"{', '.join(config.allowed_statuses)}"
        )
        if extra_note not in issues_items:
            issues_items.append(extra_note)

    issues_text = "\n".join(f"- {item}" for item in issues_items)

    rewrite = payload.get("rewrite_suggestion", "")
    if not isinstance(rewrite, str):
        rewrite = json.dumps(rewrite, ensure_ascii=False)

    is_valid = bool(payload.get("is_valid", False))
    if config.allowed_statuses and entry.status_text not in config.allowed_statuses:
        is_valid = False

    source_url = sheets_client.build_row_url(entry.row_number)

    return ValidationResult(
        row_number=entry.row_number,
        source_url=source_url,
        is_valid=is_valid,
        issues=issues_text,
        rewrite_suggestion=rewrite,
        raw_response=payload,
    )


def validate_entry(
    entry: StatusEntry,
    config: AppConfig,
    sheets_client: GoogleSheetsClient,
    llm_client: LLMClient,
) -> ValidationResult:
    messages = build_validation_messages(entry, config.rules_text, config.allowed_statuses)
    payload = llm_client.generate(messages)

    return build_result_from_payload(entry, payload, config, sheets_client)


def results_to_rows(
    entries: List[StatusEntry],
    results: List[ValidationResult],
    columns: ColumnsConfig,
    *,
    include_header: bool = True,
    identifier_column_present: bool | None = None,
    project_manager_column_present: bool | None = None,
    check_dates: Sequence[str | None] | None = None,
    model_names: Sequence[str | None] | None = None,
) -> List[List[str]]:
    default_check_date: str | None = None
    if check_dates is None:
        default_check_date = datetime.now().strftime("%d.%m.%Y %H:%M")

    header = ["Row Number"]
    identifier_key = columns.identifier
    manager_key = columns.project_manager
    if identifier_column_present is None:
        identifier_column_present = bool(identifier_key) and any(
            identifier_key in entry.source_values for entry in entries
        )
    else:
        identifier_column_present = bool(identifier_key) and identifier_column_present

    if project_manager_column_present is None:
        project_manager_column_present = bool(manager_key) and any(
            manager_key in entry.source_values for entry in entries
        )
    else:
        project_manager_column_present = bool(manager_key) and project_manager_column_present

    include_identifier = identifier_column_present
    include_manager = project_manager_column_present
    if include_identifier:
        header_label = "Project name"
        header.append(header_label)
        if include_manager:
            header.append("Project manager")
    else:
        header.append("Source URL")
        if include_manager:
            header.append("Project manager")
    header.extend(
        [
            "Status Value",
            "Completion Date",
            "Comment",
            "Is Valid",
            "Issues",
            "Rewrite Suggestion",
            "Raw LLM JSON",
            "Check date",
            "Model",
        ]
    )

    rows: List[List[str]] = []
    if include_header:
        rows.append(header)
    for index, (entry, result) in enumerate(zip(entries, results)):
        row = [str(result.row_number)]
        if include_identifier:
            hyperlink = _make_hyperlink(result.source_url, entry.identifier or "")
            row.append(hyperlink)
            if include_manager:
                row.append(entry.project_manager or "")
        else:
            row.append(result.source_url)
            if include_manager:
                row.append(entry.project_manager or "")
        if check_dates is None:
            check_date_value = default_check_date
        else:
            check_date_value = check_dates[index] if index < len(check_dates) else None
        if model_names is None:
            model_value = None
        else:
            model_value = model_names[index] if index < len(model_names) else None
        row.extend(
            [
                entry.status_text,
                entry.completion_date or "",
                entry.comment_text,
                "YES" if result.is_valid else "NO",
                result.issues,
                result.rewrite_suggestion,
                json.dumps(result.raw_response, ensure_ascii=False, indent=2),
                check_date_value or "",
                model_value or "",
            ]
        )
        rows.append(row)
    return rows
