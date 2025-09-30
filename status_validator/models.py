from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(slots=True)
class StatusEntry:
    """Normalized data for a single status row coming from Google Sheets."""

    row_index: int  # zero-based index inside the sheet data block
    row_number: int  # spreadsheet 1-based row number (including header)
    status_text: str
    comment_text: str
    completion_date: Optional[str]
    identifier: Optional[str]
    project_manager: Optional[str]
    source_values: Dict[str, Any]


@dataclass(slots=True)
class ValidationResult:
    """Result returned by the LLM for a given status entry."""

    row_number: int
    source_url: str
    is_valid: bool
    issues: str
    rewrite_suggestion: str
    raw_response: Dict[str, Any]
