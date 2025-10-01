# Status Validator

Python tool that reads project status updates from Google Sheets, validates them with an LLM, and writes structured review results back to a separate sheet.

## Table of Contents
- [English Version](#english-version)
  - [Overview](#overview)
  - [Features](#features)
  - [Requirements](#requirements)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Usage](#usage)
  - [Command-line Flags](#command-line-flags)
  - [Remote Deployment](#remote-deployment)
  - [Scheduled Runs](#scheduled-runs)
    - [Cron](#cron)
    - [systemd timer](#systemd-timer)
  - [Output](#output)
  - [Local Validation](#local-validation)
- [Русская версия](#русская-версия)
  - [Краткое описание](#краткое-описание)
  - [Возможности](#возможности)
  - [Требования](#требования)
  - [Установка](#установка)
  - [Конфигурация](#конфигурация)
  - [Запуск](#запуск)
  - [Опции командной строки](#опции-командной-строки)
  - [Удаленное развертывание](#удаленное-развертывание)
  - [Планирование запуска](#планирование-запуска)
    - [Cron](#cron-1)
    - [systemd timer](#systemd-timer-1)
  - [Результаты](#результаты)
  - [Локальная проверка](#локальная-проверка)

## English Version

### Overview
Status Validator automates the review of project status updates: it pulls rows from a Google Sheet, evaluates them with an LLM, and writes structured feedback to a results sheet.

### Features
- Loads status text and comments from a configurable Google Sheet.
- Sends each row to an LLM together with custom rules and allowed statuses.
- Captures model feedback, including rewrite suggestions.
- Publishes findings to a target sheet with hyperlinks to the original rows.
- Reuses cached LLM answers while status and comment remain unchanged.

### Requirements
- Python 3.10 or newer.
- Google service account with access to the source and target spreadsheets.
- OpenAI-compatible API key with access to the chosen model.

### Installation

```bash
pip install -e .
```

### Configuration
Create a YAML file (see `config.example.yaml`) that describes sheets, columns, validation rules, and LLM settings. Example:

```yaml
sheets:
  credentials_file: /path/to/service-account.json
  source_spreadsheet_id: 1AbCdEfGhIjKlMn
  source_sheet_name: Statuses
  source_sheet_gid: 123456789  # optional, enables direct row links
  target_spreadsheet_id: 1ZyXwVuTsRqPoNm
  target_sheet_name: Status Review
  rules_sheet_name: Rules
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
  (Paste the full validation rulebook here. It will be passed to the LLM verbatim.)
header_row: 1  # row with column titles
data_start_row: 2  # first data row (1-based) below the header
llm:
  max_retries: 3
  providers:
    1:
      name: primary  # highest priority
      model_env: OPENAI_MODEL_1
      api_key_env: OPENAI_API_KEY_1
      base_url_env: OPENAI_BASE_URL_1  # optional, overrides API host via env variable
      temperature: 0
      max_output_tokens: 1024
    2:
      name: fallback-openrouter
      model_env: OPENAI_MODEL_2
      api_key_env: OPENAI_API_KEY_2
      base_url_env: OPENAI_BASE_URL_2
      temperature: 0
      max_output_tokens: 1024
batch_size: 10
cache_path: ./build/status_cache.sqlite  # optional, defaults next to the config file
```

**Parameter reference**
- `sheets.credentials_file` — path to the Google service-account JSON key.
- `sheets.source_spreadsheet_id` — ID of the spreadsheet containing source statuses (from the URL).
- `sheets.source_sheet_name` — sheet name with source data.
- `sheets.source_sheet_gid` — numeric `gid` used to generate direct row links (optional).
- `sheets.target_spreadsheet_id` — ID of the spreadsheet used for validation results.
- `sheets.target_sheet_name` — sheet name for the result table.
- `sheets.rules_sheet_name` — sheet name where the full rules text is written when requested.
- `columns.status` — column containing the status text.
- `columns.comment` — column with the explanatory comment.
- `columns.completion_date` — column with completion dates (optional).
- `columns.identifier` — column containing a unique project name; enables updates by identifier.
- `columns.project_manager` — column with the responsible manager’s name (optional).
- `allowed_statuses` — list of valid status values; anything else is treated as invalid.
- `rules_text` — full validation rulebook supplied verbatim to the LLM.
- `header_row` — row number (1-based) containing column headers.
- `data_start_row` — first data row number (1-based) below the header.
- `llm.max_retries` — number of retries per provider before failing over to the next one.
- `llm.providers` — mapping of numeric priorities to provider profiles; lower numbers run first.
- `llm.providers.N.model_env` — env var that stores the model identifier for provider `N` (or specify `model` directly).
- `llm.providers.N.api_key_env` — env var that stores the API key for provider `N` (or specify `api_key` directly).
- `llm.providers.N.base_url_env` — env var with a custom API host for provider `N` (optional).
- `llm.providers.N.temperature` — sampling temperature for provider `N`; keep `0` for deterministic outputs.
- `llm.providers.N.max_output_tokens` — maximum number of tokens returned by provider `N`.
- `batch_size` — number of rows processed per LLM batch.
- `cache_path` — path to the SQLite cache; defaults to a file next to the YAML config.

> Place each API key in the environment variable declared in the corresponding provider block (for example `llm.providers.1.api_key_env` → `OPENAI_API_KEY_1`). Provide `OPENAI_API_KEY_2`, `OPENAI_API_KEY_3`, ... for fallback providers as needed.
> Environment variables from a `.env` file located in the working directory or next to the config file are loaded automatically.
> Set `data_start_row` to the first data row number (1-based); the row above must contain the headers.

### Usage

```bash
status-validator --config config.yaml
```

### Command-line Flags
- `--dry-run` prints the result table to stdout instead of writing to Google Sheets.
- `--limit N` processes only the first `N` rows (useful for testing).
- `--verbose` enables debug-level logging.
- `--force` ignores the cache and revalidates every row.
- `--checkdate` revalidates rows whose "Check date" is missing, invalid, or older than the current week.
- `--rules` copies the configured rules text to the dedicated sheet after validation.

### Remote Deployment
1. Provision Python 3.10+ and gather credentials (service-account JSON and `config.yaml`).
2. Clone the repository on the target host:

   ```bash
   git clone https://github.com/gihar/StatusValidator.git
   cd StatusValidator
   ```

3. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -e .
   ```

4. Configure environment variables (for example `OPENAI_API_KEY`) in a `.env` file and ensure `config.yaml` points to the deployed paths.
5. Perform a dry run to confirm connectivity:

   ```bash
   venv/bin/status-validator --config /path/to/config.yaml --dry-run --limit 5
   ```

### Scheduled Runs
Create `run_status_validator.sh` to activate the virtual environment, load `.env`, and launch the CLI. Then pick a scheduler.

#### Cron

```
0 7 * * 1-5 /home/user/StatusValidator/run_status_validator.sh
```

#### systemd timer
Create `/etc/systemd/system/status-validator.service` and `/etc/systemd/system/status-validator.timer`, then run:

```bash
sudo systemctl enable --now status-validator.timer
```

### Output
Each validated row produces the following columns:
- Row number and direct link back to the original entry.
- Project name (from `columns.identifier`) rendered as a hyperlink when available.
- Original status, comment, and completion date.
- Validation flag (`YES`/`NO`).
- Bullet list with LLM remarks.
- Rewrite suggestion aligned with the rulebook.
- Raw LLM JSON for traceability.
- "Check date" timestamp plus the LLM model identifier.

### Local Validation

```bash
python -m compileall status_validator
```

Add automated tests under `tests/` and run `pytest` as needed.

## Русская версия

### Краткое описание
Status Validator автоматизирует проверку статусов проектов: загружает строки из Google Sheets, валидирует их с помощью LLM и записывает структурированную обратную связь на лист с результатами.

### Возможности
- Загружает статусы и комментарии из настраиваемой Google-таблицы.
- Передает каждую строку в LLM вместе с правилами и допустимыми статусами.
- Фиксирует замечания модели, включая предложения по переписыванию текста.
- Публикует результаты на целевом листе с гиперссылками на исходные строки.
- Переиспользует кэшированные ответы LLM, пока статус и комментарий не меняются.

### Требования
- Python 3.10 или новее.
- Сервисный аккаунт Google с доступом к исходной и целевой таблицам.
- API-ключ, совместимый с OpenAI, с доступом к выбранной модели.

### Установка

```bash
pip install -e .
```

### Конфигурация
Создайте YAML-файл (см. `config.example.yaml`), в котором описаны таблицы, колонки, правила проверки и настройки LLM. Пример:

```yaml
sheets:
  credentials_file: /path/to/service-account.json
  source_spreadsheet_id: 1AbCdEfGhIjKlMn
  source_sheet_name: Statuses
  source_sheet_gid: 123456789  # optional, enables direct row links
  target_spreadsheet_id: 1ZyXwVuTsRqPoNm
  target_sheet_name: Status Review
  rules_sheet_name: Rules
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
  (Paste the full validation rulebook here. It will be passed to the LLM verbatim.)
header_row: 1  # row with column titles
data_start_row: 2  # first data row (1-based) below the header
llm:
  max_retries: 3
  providers:
    1:
      name: primary  # наивысший приоритет
      model_env: OPENAI_MODEL_1
      api_key_env: OPENAI_API_KEY_1
      base_url_env: OPENAI_BASE_URL_1  # опционально, для смены хоста API
      temperature: 0
      max_output_tokens: 1024
    2:
      name: fallback-openrouter
      model_env: OPENAI_MODEL_2
      api_key_env: OPENAI_API_KEY_2
      base_url_env: OPENAI_BASE_URL_2
      temperature: 0
      max_output_tokens: 1024
batch_size: 10
cache_path: ./build/status_cache.sqlite  # optional, defaults next to the config file
```

**Справочник параметров**
- `sheets.credentials_file` — путь к JSON-ключу сервисного аккаунта Google.
- `sheets.source_spreadsheet_id` — идентификатор таблицы со статусами (из URL).
- `sheets.source_sheet_name` — лист с исходными данными.
- `sheets.source_sheet_gid` — числовой `gid`, позволяющий формировать прямые ссылки на строки (опционально).
- `sheets.target_spreadsheet_id` — идентификатор таблицы с результатами проверки.
- `sheets.target_sheet_name` — лист, куда записываются результаты.
- `sheets.rules_sheet_name` — лист, куда при необходимости выгружается полный текст правил.
- `columns.status` — колонка со статусом.
- `columns.comment` — колонка с пояснительным комментарием.
- `columns.completion_date` — колонка с датой завершения (опционально).
- `columns.identifier` — колонка с уникальным названием проекта; позволяет обновлять строки по идентификатору.
- `columns.project_manager` — колонка с именем ответственного менеджера (опционально).
- `allowed_statuses` — список допустимых значений статуса; другие значения считаются некорректными.
- `rules_text` — полный набор правил проверки, передаваемый LLM без изменений.
- `header_row` — номер строки (с единицы), в которой находятся заголовки.
- `data_start_row` — номер первой строки с данными под заголовками (с единицы).
- `llm.max_retries` — количество попыток на один провайдер перед переключением к следующему.
- `llm.providers` — отображение «приоритет → настройки провайдера»; сначала используется профиль с меньшим номером.
- `llm.providers.N.model_env` — имя переменной окружения с идентификатором модели для провайдера `N` (или задайте `model` явно).
- `llm.providers.N.api_key_env` — имя переменной с API-ключом для провайдера `N` (или задайте `api_key` напрямую).
- `llm.providers.N.base_url_env` — имя переменной с альтернативным базовым URL для провайдера `N` (опционально).
- `llm.providers.N.temperature` — температура выборки для провайдера `N`; оставьте `0` для детерминированных ответов.
- `llm.providers.N.max_output_tokens` — максимальное число токенов в ответе провайдера `N`.
- `batch_size` — количество строк, обрабатываемых за один батч LLM.
- `cache_path` — путь к файлу SQLite-кэша; по умолчанию создается рядом с YAML-файлом.

> Разместите API-ключи в переменных, указанных в соответствующих блоках провайдеров (например, `llm.providers.1.api_key_env` → `OPENAI_API_KEY_1`). Для резервных профилей добавьте `OPENAI_API_KEY_2`, `OPENAI_API_KEY_3` и т.д.
> Переменные окружения из `.env` в рабочей директории или рядом с конфигом подхватываются автоматически.
> `data_start_row` должен указывать на первую строку данных; строка выше содержит заголовки.

### Запуск

```bash
status-validator --config config.yaml
```

### Опции командной строки
- `--dry-run` — выводит таблицу результатов в stdout вместо записи в Google Sheets.
- `--limit N` — обрабатывает только первые `N` строк (удобно для тестирования).
- `--verbose` — включает детализированный лог.
- `--force` — игнорирует кэш и повторно валидирует каждую строку.
- `--checkdate` — принудительно проверяет строки с пустой, некорректной или устаревшей датой в колонке "Check date".
- `--rules` — после проверки копирует текст правил на отдельный лист в таблице результатов.

### Удаленное развертывание
1. Установите Python 3.10+ и подготовьте учетные данные (JSON сервисного аккаунта и `config.yaml`).
2. Клонируйте репозиторий на сервер:

   ```bash
   git clone https://github.com/gihar/StatusValidator.git
   cd StatusValidator
   ```

3. Создайте виртуальное окружение и установите зависимости:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -e .
   ```

4. Настройте переменные окружения (например, `OPENAI_API_KEY`) в `.env` и убедитесь, что пути в `config.yaml` актуальны.
5. Выполните пробный запуск:

   ```bash
   venv/bin/status-validator --config /path/to/config.yaml --dry-run --limit 5
   ```

### Планирование запуска
Создайте скрипт `run_status_validator.sh`, который активирует виртуальное окружение, загружает `.env` и запускает CLI, затем выберите планировщик.

#### Cron

```
0 7 * * 1-5 /home/user/StatusValidator/run_status_validator.sh
```

#### systemd timer
Создайте `/etc/systemd/system/status-validator.service` и `/etc/systemd/system/status-validator.timer`, после чего выполните:

```bash
sudo systemctl enable --now status-validator.timer
```

### Результаты
Каждая проверенная строка формирует следующие колонки:
- Номер строки и прямая ссылка на исходную запись.
- Название проекта (из `columns.identifier`) в виде гиперссылки при наличии значения.
- Исходный статус, комментарий и дата завершения.
- Флаг валидации (`YES`/`NO`).
- Список замечаний от LLM.
- Предложение по переписыванию, соответствующее правилам.
- Сырой JSON от LLM для аудита.
- Отметка времени в колонке "Check date" и идентификатор модели в колонке "Model".

### Локальная проверка

```bash
python -m compileall status_validator
```

При необходимости добавьте автотесты в каталог `tests/` и запустите `pytest`.
