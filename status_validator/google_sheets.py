from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Sequence

import logging
import ssl
import time

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import Resource
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from httplib2 import HttpLib2Error

from .config import SheetsConfig

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

LOGGER = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_ATTEMPTS = 4
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 8.0
_RETRYABLE_EXCEPTIONS = (ssl.SSLEOFError, HttpLib2Error)


class GoogleSheetsClient:
    """Thin wrapper around the Google Sheets API for this project."""

    def __init__(self, conf: SheetsConfig) -> None:
        self._conf = conf
        self._service: Resource | None = None

    def _service_client(self) -> Resource:
        if self._service is None:
            creds = Credentials.from_service_account_file(
                str(self._conf.credentials_file), scopes=SCOPES
            )
            self._service = build("sheets", "v4", credentials=creds)
        return self._service

    # Reading -----------------------------------------------------------------
    def fetch_values(self) -> List[List[str]]:
        """Load all values from the configured source sheet."""

        def _build_request() -> HttpRequest:
            service = self._service_client()
            return (
                service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self._conf.source_spreadsheet_id,
                    range=self._conf.source_sheet_name,
                )
            )

        result = self._execute_with_retry(_build_request, operation="fetch source values")
        return result.get("values", [])

    def fetch_target_values(self) -> List[List[str]]:
        """Load all values from the configured target sheet."""

        def _build_request() -> HttpRequest:
            service = self._service_client()
            return (
                service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self._conf.target_spreadsheet_id,
                    range=self._conf.target_sheet_name,
                )
            )

        result = self._execute_with_retry(_build_request, operation="fetch target values")
        return result.get("values", [])

    # Writing -----------------------------------------------------------------
    def overwrite_results(self, values: Iterable[Iterable[str]]) -> None:
        """Replace target sheet data with provided rows (header row included)."""

        payload = {
            "values": [list(row) for row in values],
        }
        target_range = f"{self._conf.target_sheet_name}!A1"

        def _clear_request() -> HttpRequest:
            service = self._service_client()
            return (
                service.spreadsheets()
                .values()
                .clear(
                    spreadsheetId=self._conf.target_spreadsheet_id,
                    range=self._conf.target_sheet_name,
                )
            )

        def _update_request() -> HttpRequest:
            service = self._service_client()
            return (
                service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self._conf.target_spreadsheet_id,
                    range=target_range,
                    valueInputOption="USER_ENTERED",
                    body=payload,
                )
            )

        self._execute_with_retry(_clear_request, operation="clear target sheet")
        self._execute_with_retry(_update_request, operation="overwrite target sheet")

    def append_results(self, values: Iterable[Iterable[str]]) -> dict | None:
        """Append rows to the target sheet without removing existing data."""

        rows = [list(row) for row in values]
        if not rows:
            return None

        payload = {"values": rows}
        target_range = f"{self._conf.target_sheet_name}!A1"

        def _append_request() -> HttpRequest:
            service = self._service_client()
            return (
                service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self._conf.target_spreadsheet_id,
                    range=target_range,
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body=payload,
                )
            )

        return self._execute_with_retry(_append_request, operation="append rows")

    def update_target_header(self, header: Sequence[str]) -> None:
        """Ensure the target sheet header matches the expected columns."""

        payload = {"values": [list(header)]}
        target_range = f"{self._conf.target_sheet_name}!A1"

        def _update_request() -> HttpRequest:
            service = self._service_client()
            return (
                service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self._conf.target_spreadsheet_id,
                    range=target_range,
                    valueInputOption="USER_ENTERED",
                    body=payload,
                )
            )

        self._execute_with_retry(_update_request, operation="update target header")

    def update_target_rows(self, row_updates: Dict[int, Sequence[str]]) -> None:
        """Batch update rows in the target sheet by their 1-based positions."""

        if not row_updates:
            return

        data = []
        for row_number, values in sorted(row_updates.items()):
            if row_number < 1:
                msg = f"Row numbers must be 1-based; received {row_number}"
                raise ValueError(msg)
            data.append(
                {
                    "range": f"{self._conf.target_sheet_name}!A{row_number}",
                    "majorDimension": "ROWS",
                    "values": [list(values)],
                }
            )

        def _batch_update_request() -> HttpRequest:
            service = self._service_client()
            return (
                service.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=self._conf.target_spreadsheet_id,
                    body={
                        "valueInputOption": "USER_ENTERED",
                        "data": data,
                    },
                )
            )

        self._execute_with_retry(_batch_update_request, operation="update target rows")

    # Helpers -----------------------------------------------------------------
    def build_row_url(self, row_number: int) -> str:
        """Create a direct Google Sheets URL pointing to a specific row."""

        gid = self._conf.source_sheet_gid
        gid_part = f"#gid={gid}" if gid is not None else ""
        return (
            f"https://docs.google.com/spreadsheets/d/{self._conf.source_spreadsheet_id}/edit"
            f"{gid_part}&range={row_number}:{row_number}"
        )

    # Internal ----------------------------------------------------------------
    def _reset_service(self) -> None:
        self._service = None

    def _execute_with_retry(
        self,
        request_builder: Callable[[], HttpRequest],
        *,
        operation: str,
    ) -> dict:
        """Execute a Sheets API request with retries for transient failures."""

        backoff = _INITIAL_BACKOFF_SECONDS
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
            try:
                return request_builder().execute()
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status not in _RETRYABLE_STATUS_CODES:
                    raise
                last_exc = exc

            if attempt == _MAX_RETRY_ATTEMPTS:
                raise last_exc

            wait_time = min(backoff, _MAX_BACKOFF_SECONDS)
            LOGGER.warning(
                "Sheets API %s failed on attempt %s/%s (%s); retrying in %.1f seconds",
                operation,
                attempt,
                _MAX_RETRY_ATTEMPTS,
                last_exc,
                wait_time,
            )
            self._reset_service()
            time.sleep(wait_time)
            backoff *= 2

        # If the loop exits without returning, re-raise the last exception.
        if last_exc is not None:  # pragma: no cover - belt and suspenders.
            raise last_exc
        raise RuntimeError("Sheets API request failed without capturing an exception")
