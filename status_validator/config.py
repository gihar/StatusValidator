from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class ColumnsConfig(BaseModel):
    status: str = Field(..., description="Name of the column that stores project status text")
    comment: str = Field(..., description="Name of the column that stores status comment text")
    completion_date: Optional[str] = Field(
        None, description="Optional column containing planned or actual completion date"
    )
    identifier: Optional[str] = Field(
        None,
        description="Optional column used as a human friendly identifier (e.g. project name)",
    )
    project_manager: Optional[str] = Field(
        None,
        description="Optional column that stores project manager name",
    )


class SheetsConfig(BaseModel):
    credentials_file: Path = Field(
        ..., description="Path to the Google service account JSON credentials"
    )
    source_spreadsheet_id: str = Field(..., description="ID of the spreadsheet with source data")
    source_sheet_name: str = Field(..., description="Tab name in the source spreadsheet")
    source_sheet_gid: Optional[int] = Field(
        None,
        description="Optional gid of the source sheet used to build direct row links",
    )
    target_spreadsheet_id: str = Field(..., description="ID of the spreadsheet for output data")
    target_sheet_name: str = Field(..., description="Tab name in the target spreadsheet")
    rules_sheet_name: Optional[str] = Field(
        None,
        description="Optional tab name in the target spreadsheet used to store validation rules",
    )

    @field_validator("credentials_file")
    @classmethod
    def _expand_credentials_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()


class LLMProviderConfig(BaseModel):
    """Settings for a single prioritized LLM provider."""

    name: str | None = Field(
        None,
        description="Human-friendly name for the provider; used for logging",
    )
    model: str | None = Field(
        None,
        description="LLM model identifier; optional when model_env is provided",
    )
    model_env: str | None = Field(
        None,
        description="Environment variable with the model identifier",
    )
    temperature: float = Field(
        0.0,
        ge=0.0,
        le=2.0,
        description="Sampling temperature for this provider",
    )
    max_output_tokens: int = Field(
        1024,
        gt=0,
        description="Maximum number of tokens returned by the provider",
    )
    api_key: str | None = Field(
        None,
        description="Explicit API key; if omitted the key is read from api_key_env",
    )
    api_key_env: str | None = Field(
        None,
        description="Environment variable with the API key",
    )
    base_url: str | None = Field(
        None,
        description="Optional override for the API base URL",
    )
    base_url_env: str | None = Field(
        None,
        description="Environment variable name for the API base URL",
    )
    organization: str | None = Field(
        None,
        description="Optional OpenAI organization identifier",
    )
    request_timeout: int = Field(
        60,
        gt=0,
        description="Timeout in seconds for API requests",
    )
    reasoning_enabled: bool = Field(
        True,
        description="Include reasoning effort hints when calling this provider",
    )

    @model_validator(mode="after")
    def _ensure_required_fields(self) -> "LLMProviderConfig":
        if not self.model and not self.model_env:
            raise ValueError("LLM provider must define 'model' or 'model_env'")
        if not self.api_key and not self.api_key_env:
            raise ValueError("LLM provider must define 'api_key' or 'api_key_env'")
        return self


class LLMConfig(BaseModel):
    max_retries: int = Field(
        3,
        ge=1,
        description="Number of attempts per provider before switching to the next one",
    )
    max_workers: int = Field(
        1,
        ge=1,
        le=20,
        description="Number of parallel threads for LLM requests (1 = sequential, 5 recommended)",
    )
    http_referer: str | None = Field(
        None,
        description="HTTP Referer header sent for OpenRouter app attribution",
    )
    x_title: str | None = Field(
        None,
        description="X-Title header sent for OpenRouter app attribution",
    )
    providers: dict[int, LLMProviderConfig] = Field(
        ...,
        description="Mapping of priority -> provider configuration",
    )

    @field_validator("providers")
    @classmethod
    def _validate_providers(
        cls, value: dict[int, LLMProviderConfig]
    ) -> dict[int, LLMProviderConfig]:
        if not value:
            raise ValueError("At least one LLM provider must be configured")

        ordered_items = sorted(value.items(), key=lambda item: item[0])
        priorities = [priority for priority, _ in ordered_items]

        expected = list(range(1, len(ordered_items) + 1))
        if priorities != expected:
            raise ValueError(
                "LLM provider priorities must be consecutive integers starting from 1"
            )

        return dict(ordered_items)

    @property
    def provider_sequence(self) -> List[tuple[int, LLMProviderConfig]]:
        return list(self.providers.items())


class AppConfig(BaseModel):
    sheets: SheetsConfig
    columns: ColumnsConfig
    header_row: int = Field(
        1,
        ge=1,
        description="1-based row number that contains the column headers",
    )
    data_start_row: int = Field(
        2,
        ge=1,
        description="1-based row number where table data begins",
    )
    allowed_statuses: List[str] = Field(default_factory=list)
    rules_text: str = Field(..., description="Full text of the validation rules")
    llm: LLMConfig
    batch_size: int = Field(10, gt=0, description="Number of rows processed per batch")
    cache_path: Path | None = Field(
        None,
        description="Optional path to SQLite cache file for cached LLM responses",
    )

    @model_validator(mode="after")
    def _validate_rows(self) -> "AppConfig":
        if self.data_start_row <= self.header_row:
            msg = "data_start_row must be greater than header_row"
            raise ValueError(msg)
        return self

    @field_validator("cache_path")
    @classmethod
    def _expand_cache_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser().resolve()


def load_config(path: str | Path) -> AppConfig:
    """Load configuration from a YAML file and return a validated object."""

    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        msg = f"Configuration file not found: {config_path}"
        raise FileNotFoundError(msg)

    with config_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if data is None:
        msg = f"Configuration file is empty: {config_path}"
        raise ValueError(msg)

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:  # pragma: no cover - passthrough for readability
        raise ValueError(f"Invalid configuration: {exc}") from exc
