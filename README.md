# Status Validator

Python utility that reads project status updates from Google Sheets, evaluates them with an LLM, and writes structured feedback back to a results sheet. The tool ships with caching, incremental updates, and optional parallel execution.

- 🇬🇧 English (this document)
- 🇷🇺 [Русская версия](#-русская-версия)

## Overview
Status Validator pulls rows from a configured Google Sheet, prepares an optimized JSON-only prompt, and sends it to an OpenAI-compatible model. Each response is converted into a structured record that can either overwrite or append to the target sheet. Cached payloads, incremental updates, and retry logic keep runs fast and predictable.

## Key Capabilities
- Reads source data, rules, and identifiers from Google Sheets using a service account.
- Builds JSON-only prompts, enforces retries, and supports multiple LLM providers with priority fallback.
- Persists LLM responses in a SQLite cache; skips revalidation unless the status text or comment changes.
- Updates existing result rows by project identifier or appends new rows when no identifier is present.
- Supports prompt-cache-friendly requests (`prompt_cache_key`) and optional parallel validation via `ThreadPoolExecutor`.
- Emits detailed logging, including cache hits, prompt caching metrics, and rate-limit retries.

## Requirements
- Python 3.10 or newer.
- Google Cloud service-account JSON with access to both the source and target spreadsheets.
- OpenAI-compatible API key (e.g., OpenAI, OpenRouter, Groq) with access to the selected model.

## Quick Start
1. Clone the repository and create a virtual environment:
   ```bash
   git clone https://github.com/gihar/StatusValidator.git
   cd StatusValidator
   python3 -m venv venv
   source venv/bin/activate
   ```
2. Install the package (editable install recommended for local changes):
   ```bash
   pip install -e .
   ```
   For development extras run `pip install -e .[dev]`.
3. Copy `config.example.yaml` to `config.yaml` and adjust it to match your spreadsheets.
4. Place service-account credentials and LLM API keys in the paths and environment variables referenced by the config. `.env` files in the project root or next to `config.yaml` are loaded automatically.
5. Run a dry test:
   ```bash
   status-validator --config config.yaml --dry-run --limit 5 --verbose
   ```
   Remove `--dry-run` once the output looks correct.

## Configuration Reference
Example configuration:
```yaml
sheets:
  credentials_file: ./service-account.json
  source_spreadsheet_id: 1AbCdEfGhIjKlMn
  source_sheet_name: Statuses
  source_sheet_gid: 123456789   # optional, allows direct row links
  target_spreadsheet_id: 1ZyXwVuTsRqPoNm
  target_sheet_name: Status Review
  rules_sheet_name: Rules       # optional destination for --rules flag
columns:
  status: Статус
  comment: Комментарий
  completion_date: Дата завершения
  identifier: Наименование проекта
  project_manager: Руководитель проекта
allowed_statuses:
  - В графике
  - Есть риски
  - Отстает
rules_text: |
  Полный свод правил, передаваемый LLM без изменений.
header_row: 1
data_start_row: 2
llm:
  max_retries: 3
  max_workers: 5
  providers:
    1:
      name: primary
      model_env: OPENAI_MODEL_1
      api_key_env: OPENAI_API_KEY_1
      base_url_env: OPENAI_BASE_URL_1
      temperature: 0
      max_output_tokens: 1024
    2:
      name: fallback-openrouter
      model_env: OPENAI_MODEL_2
      api_key_env: OPENAI_API_KEY_2
batch_size: 10
cache_path: ./build/status_validator_cache.sqlite
```

Key fields:

| Key | Purpose |
| --- | --- |
| `sheets.credentials_file` | Path to the Google service-account JSON file. |
| `sheets.source_spreadsheet_id` / `source_sheet_name` | Identify the sheet that stores raw statuses. |
| `sheets.target_spreadsheet_id` / `target_sheet_name` | Destination for validation results. |
| `sheets.source_sheet_gid` | Enables direct row links in the "Row Number" column (optional). |
| `sheets.rules_sheet_name` | Target tab that receives `rules_text` when `--rules` is used. |
| `columns.*` | Column headers exactly as they appear in the source sheet. Optional fields can be left empty. |
| `allowed_statuses` | Restricts valid status values; anything outside the list becomes a validation error. |
| `rules_text` | Multi-line rulebook passed verbatim to the LLM and used to compute the prompt cache key. |
| `header_row` / `data_start_row` | 1-based positions of the header row and the first data row. `data_start_row` must be greater than `header_row`. |
| `llm.max_retries` | Attempts per provider before falling back to the next one. |
| `llm.max_workers` | Number of parallel threads for validation (1 = sequential). |
| `llm.providers[].reasoning_enabled` | Request high-effort reasoning from this provider when it supports extended reasoning hints. |
| `llm.http_referer` | Optional HTTP referer header for OpenRouter app attribution. |
| `llm.x_title` | Optional X-Title header for OpenRouter app attribution. |
| `llm.providers` | Priority-ordered providers; keys must be consecutive integers starting from 1. Each provider can read credentials and model IDs from env vars. |
| `batch_size` | Chunk size for processing entries. Smaller batches reduce memory usage; larger batches improve cache locality. |
| `cache_path` | Location of the SQLite cache file. Defaults to `status_validator_cache.sqlite` next to the config when omitted. |

## Running the Validator
```bash
status-validator --config /path/to/config.yaml
```

The CLI loads `.env` from the current directory first and then from the directory that contains the config file. Important flags:

| Flag | Description |
| --- | --- |
| `--dry-run` | Skip Google Sheets writes; dump the resulting table to stdout as JSON. |
| `--limit N` | Process only the first `N` data rows. Useful for smoke tests. |
| `--verbose` | Enable debug logging (prompt caching metrics, retries, cache hits). |
| `--force` | Ignore cached payloads and revalidate every row. |
| `--checkdate` | Revalidate rows whose "Check date" column is empty, invalid, or from a previous week. |
| `--rules` | After validation, write `rules_text` into `sheets.rules_sheet_name`. |

## Output Schema
Each processed entry produces:
- `Row Number` with a hyperlink to the source row when `source_sheet_gid` is configured.
- Either `Project name` (hyperlinked identifier) or `Source URL`, plus `Project manager` when the column exists.
- `Status Value`, `Completion Date`, and `Comment` copied from the source sheet.
- `Is Valid` (`YES`/`NO`), `Issues` (markdown-style bullets), and `Rewrite Suggestion` generated by the LLM.
- `Raw LLM JSON` serialized with indentation for audit purposes.
- `Check date` timestamp and `Model` name showing when and by which model the row was processed.

## Prompt Caching and Parallel Execution
- **SQLite cache:** `cache.CacheStore` stores the full LLM JSON response keyed by row number, status text, and comment hash. Reuse is automatic unless `--force` or `--checkdate` says otherwise.
- **OpenAI automatic prompt caching:** `prompt_cache_key` is passed for every request. When supported (GPT-4o, GPT-4o-mini, o1 models) the API returns cache hit statistics in the log (`Prompt cache hit: 2048/2500 tokens (81.9%)`).
- **Parallel workers:** Set `llm.max_workers > 1` to validate rows concurrently. Rate-limit errors trigger exponential backoff and retry inside `parallel.validate_batch_parallel`.

More background material is available in `PROMPT_CACHING.md` and `docs/multithreading/`.

## Operational Tips
- `--checkdate` compares the stored "Check date" to the current ISO week. Stale or missing dates trigger revalidation while fresh rows reuse the cache.
- When `columns.identifier` is populated, the validator updates existing rows by matching the identifier, preserving results order and avoiding duplicates.
- Use `batch_size` to balance throughput and API quotas. Ten-row batches work well when prompt caching is active.
- Set `llm.providers.N.base_url_env` and `...api_key_env` to integrate with non-OpenAI compatible endpoints such as OpenRouter or Groq.
- The rules sheet update (`--rules`) clears the destination tab before writing one rule per row.

## Automation
Create a wrapper script that activates the virtual environment, loads environment variables, and calls the CLI. Schedule it via cron or `systemd`:

Cron example:
```
0 7 * * 1-5 /home/user/StatusValidator/run_status_validator.sh
```

`systemd` timer outline:
```
# /etc/systemd/system/status-validator.service
[Service]
ExecStart=/home/user/StatusValidator/run_status_validator.sh

# /etc/systemd/system/status-validator.timer
[Timer]
OnCalendar=Mon..Fri 07:00
Persistent=true
```
Then run `sudo systemctl enable --now status-validator.timer`.

## Local Development
- Run `pytest` to execute unit tests (install extras with `pip install -e .[dev]`).
- `python -m compileall status_validator` performs a lightweight syntax check for all modules.
- The project uses standard logging; enable `--verbose` for detailed traces during development.

## Additional Documentation
- `PROMPT_CACHING.md` — deep dive into automatic prompt caching and `prompt_cache_key`.
- `PROMPT_CACHE_KEY.md` — how the cache key is generated from `rules_text` and allowed statuses.
- `docs/multithreading/README.md` — architecture notes, benchmarks, and FAQ for parallel execution.
- `MIGRATION_TO_CACHING.md` — migration notes for enabling prompt caching in existing deployments.

---

## 🇷🇺 Русская версия
Status Validator загружает статусы проектов из Google Sheets, проверяет их с помощью LLM и записывает структурированный результат на отдельный лист. Ниже приведена полная инструкция, эквивалентная английскому разделу.

### Обзор
Программа считывает строки из настроенной Google-таблицы, формирует оптимизированный промпт в формате JSON-only и отправляет его в модель, совместимую с OpenAI. Ответ превращается в структурированные данные, которые можно перезаписать в лист результатов или добавить как новые строки. Кеширование, инкрементальные обновления и логика повторных попыток делают запуск предсказуемым и быстрым.

### Ключевые возможности
- Чтение исходных данных, правил и идентификаторов через сервисный аккаунт Google.
- Формирование JSON-only промптов, система повторных попыток и приоритетный список LLM-провайдеров.
- SQLite-кэш с привязкой к номеру строки, тексту статуса и хэшу комментария — повторная проверка выполняется только при изменении данных.
- Обновление строк по идентификатору проекта либо добавление новых записей при его отсутствии.
- Поддержка `prompt_cache_key` и многопоточной валидации через `ThreadPoolExecutor`.
- Детализированные логи: попадания в кеш, статистика prompt caching и обработка ограничений скорости.

### Требования
- Python версии 3.10 и выше.
- JSON-ключ сервисного аккаунта Google с доступом к исходной и целевой таблицам.
- API-ключ для сервиса, совместимого с OpenAI (OpenAI, OpenRouter, Groq и т.д.), с доступом к выбранной модели.

### Быстрый старт
1. Клонируйте репозиторий и создайте виртуальное окружение:
   ```bash
   git clone https://github.com/gihar/StatusValidator.git
   cd StatusValidator
   python3 -m venv venv
   source venv/bin/activate
   ```
2. Установите пакет (для локальных правок рекомендуется editable-режим):
   ```bash
   pip install -e .
   ```
   Для разработки и тестов используйте `pip install -e .[dev]`.
3. Скопируйте `config.example.yaml` в `config.yaml` и настройте идентификаторы таблиц и колонок.
4. Разместите файл сервисного аккаунта и API-ключи в путях и переменных окружения, указанных в конфигурации. Файлы `.env` в корне проекта и рядом с `config.yaml` подхватываются автоматически.
5. Выполните пробный запуск:
   ```bash
   status-validator --config config.yaml --dry-run --limit 5 --verbose
   ```
   Уберите `--dry-run`, когда убедитесь, что результат корректен.

### Справочник по конфигурации
Пример настроек:
```yaml
sheets:
  credentials_file: ./service-account.json
  source_spreadsheet_id: 1AbCdEfGhIjKlMn
  source_sheet_name: Statuses
  source_sheet_gid: 123456789   # необязательно, включает прямые ссылки на строки
  target_spreadsheet_id: 1ZyXwVuTsRqPoNm
  target_sheet_name: Status Review
  rules_sheet_name: Rules       # необязательно, используется флагом --rules
columns:
  status: Статус
  comment: Комментарий
  completion_date: Дата завершения
  identifier: Наименование проекта
  project_manager: Руководитель проекта
allowed_statuses:
  - В графике
  - Есть риски
  - Отстает
rules_text: |
  Полный свод правил, передаваемый LLM без изменений.
header_row: 1
data_start_row: 2
llm:
  max_retries: 3
  max_workers: 5
  providers:
    1:
      name: primary
      model_env: OPENAI_MODEL_1
      api_key_env: OPENAI_API_KEY_1
      base_url_env: OPENAI_BASE_URL_1
      temperature: 0
      max_output_tokens: 1024
    2:
      name: fallback-openrouter
      model_env: OPENAI_MODEL_2
      api_key_env: OPENAI_API_KEY_2
batch_size: 10
cache_path: ./build/status_validator_cache.sqlite
```

Основные поля:

| Поле | Назначение |
| --- | --- |
| `sheets.credentials_file` | Путь к JSON сервисного аккаунта Google. |
| `sheets.source_spreadsheet_id` / `source_sheet_name` | Указывают таблицу и лист с исходными статусами. |
| `sheets.target_spreadsheet_id` / `target_sheet_name` | Лист-приемник результатов проверки. |
| `sheets.source_sheet_gid` | Позволяет формировать прямые ссылки на строки (опционально). |
| `sheets.rules_sheet_name` | Лист, в который записываются правила при запуске с `--rules`. |
| `columns.*` | Названия колонок, как в исходной таблице; необязательные поля можно опустить. |
| `allowed_statuses` | Список допустимых значений статуса; иные значения считаются ошибкой. |
| `rules_text` | Полный текст правил, передается LLM без изменений и участвует в расчете ключа кеша. |
| `header_row` / `data_start_row` | Номера строк (с единицы) для заголовка и первой строки данных; `data_start_row` должен быть больше `header_row`. |
| `llm.max_retries` | Количество попыток для провайдера перед переходом к следующему. |
| `llm.max_workers` | Число параллельных потоков (1 = последовательная обработка). |
| `llm.providers[].reasoning_enabled` | Включает подсказку про «высокое усилие» для конкретного провайдера, если он поддерживает расширенное рассуждение. |
| `llm.http_referer` | Необязательный заголовок HTTP Referer для App Attribution в OpenRouter. |
| `llm.x_title` | Необязательный заголовок X-Title для App Attribution в OpenRouter. |
| `llm.providers` | Провайдеры по приоритетам; ключи — последовательные числа от 1. Параметры можно считывать из переменных окружения. |
| `batch_size` | Размер батча для обработки; большие батчи улучшают локальность кеша. |
| `cache_path` | Путь к файлу SQLite-кеша. По умолчанию создается рядом с конфигом. |

### Запуск валидатора
```bash
status-validator --config /path/to/config.yaml
```

CLI сначала загружает `.env` из текущей директории, затем из каталога, где лежит конфиг. Полезные флаги:

| Флаг | Назначение |
| --- | --- |
| `--dry-run` | Не записывает данные в Google Sheets; выводит результат в stdout (JSON). |
| `--limit N` | Обрабатывает только первые `N` строк — удобно для тестов. |
| `--verbose` | Включает расширенный лог (prompt caching, кеш, повторные попытки). |
| `--force` | Игнорирует локальный кеш и валидирует каждую строку заново. |
| `--checkdate` | Переобновляет строки с пустой, некорректной или устаревшей колонкой "Check date". |
| `--rules` | После проверки записывает `rules_text` на лист `sheets.rules_sheet_name`. |

### Структура результата
Для каждой обработанной строки формируется запись:
- `Row Number` с гиперссылкой на исходную строку (если указан `source_sheet_gid`).
- `Project name` (гиперссылка на идентификатор) или `Source URL`, а также `Project manager`, если колонка существует.
- `Status Value`, `Completion Date` и `Comment`, скопированные из исходной таблицы.
- `Is Valid` (`YES`/`NO`), `Issues` (список с маркерами) и `Rewrite Suggestion`, созданные моделью.
- `Raw LLM JSON`, сериализованный для аудита.
- `Check date` и `Model`, отражающие время обработки и название модели.

### Prompt caching и параллельная обработка
- **SQLite-кэш:** `cache.CacheStore` хранит полный JSON-ответ LLM и переиспользует его при неизменных данных, если не указаны `--force` или `--checkdate`.
- **Автоматическое prompt caching OpenAI:** параметр `prompt_cache_key` передается в каждый запрос. Для поддерживаемых моделей (GPT-4o, GPT-4o-mini, o1) в логах появляется строка вроде `Prompt cache hit: 2048/2500 tokens (81.9%)`.
- **Параллельные потоки:** установка `llm.max_workers > 1` включает параллельную валидацию. Ограничения по скорости обрабатываются экспоненциальным бэк-оффом в `parallel.validate_batch_parallel`.

Дополнительные материалы: `PROMPT_CACHING.md`, каталог `docs/multithreading/`.

### Практические советы
- Флаг `--checkdate` сравнивает значение "Check date" с текущей ISO-неделей и принудительно обновляет устаревшие записи.
- Если настроена колонка `identifier`, строки обновляются по идентификатору без дублирования и с сохранением порядка.
- Подбирайте `batch_size` с учетом квот API: батчи примерно по 10 строк хорошо сочетаются с prompt caching.
- Для сторонних API задавайте `llm.providers.N.base_url_env` и `...api_key_env` (OpenRouter, Groq и другие).
- При использовании `--rules` целевой лист очищается перед добавлением правил.

### Автоматизация
Создайте обертку, которая активирует виртуальное окружение, загружает переменные окружения и запускает CLI. Примеры расписаний:

Cron:
```
0 7 * * 1-5 /home/user/StatusValidator/run_status_validator.sh
```

`systemd` timer:
```
# /etc/systemd/system/status-validator.service
[Service]
ExecStart=/home/user/StatusValidator/run_status_validator.sh

# /etc/systemd/system/status-validator.timer
[Timer]
OnCalendar=Mon..Fri 07:00
Persistent=true
```
После этого выполните `sudo systemctl enable --now status-validator.timer`.

### Локальная разработка
- Запускайте `pytest` (предварительно `pip install -e .[dev]`) для unit-тестов.
- Команда `python -m compileall status_validator` выполняет быструю проверку синтаксиса модулей.
- Логи выводятся через стандартный модуль `logging`; для диагностики добавляйте `--verbose`.

### Дополнительная документация
- `PROMPT_CACHING.md` — детали автоматического prompt caching и ключа `prompt_cache_key`.
- `PROMPT_CACHE_KEY.md` — как формируется ключ кеша из `rules_text` и допустимых статусов.
- `docs/multithreading/README.md` — архитектура, бенчмарки и FAQ по параллельной обработке.
- `MIGRATION_TO_CACHING.md` — инструкция по миграции на prompt caching в существующих установках.
