# Status Validator

Python tool that reads project status updates from Google Sheets, validates them with an LLM, and writes structured review results back to a separate sheet.

## Features
- Loads source statuses and comments from a configurable Google Sheet.
- Sends each entry to an LLM together with the validation rules and allowed status list.
- Collects machine generated review notes and rewrite suggestions.
- Publishes the findings to a result sheet, including a direct link back to the source row.
- Caches LLM responses per row and reuses them when the status and comment stay the same.

## Requirements
- Python 3.10 or newer.
- Google service account with access to the source and target spreadsheets.
- OpenAI compatible API key with access to the requested model.

Install dependencies:

```bash
pip install -e .
```

## Configuration
Create a YAML file (see `config.example.yaml`) that describes the sheets, columns, validation rules, and LLM settings. Example:

```yaml
sheets:
  credentials_file: /path/to/service-account.json
  source_spreadsheet_id: 1AbCdEfGhIjKlMn
  source_sheet_name: Statuses
  source_sheet_gid: 123456789  # optional, enables direct row links
  target_spreadsheet_id: 1ZyXwVuTsRqPoNm
  target_sheet_name: Status Review
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
  model_env: OPENAI_MODEL  # or set `model` directly
  temperature: 0
  max_output_tokens: 1024
  api_key_env: OPENAI_API_KEY
  base_url_env: OPENAI_BASE_URL  # optional, overrides API host via env variable
batch_size: 10
cache_path: ./build/status_cache.sqlite  # optional, defaults next to the config file
```

> Place the OpenAI API key in the environment variable declared in `api_key_env` (default `OPENAI_API_KEY`).
> Environment variables from a `.env` file located in the working directory or next to the config file are loaded automatically, so you can keep secrets out of the YAML.
> Set `data_start_row` to the first data row number (1-based). The row immediately above must contain the column headers used for mapping.
> If the headers are located below the first row, adjust `header_row` accordingly.

## Usage
Run the CLI after preparing the configuration file:

```bash
status-validator --config config.yaml
```

Useful flags:
- `--dry-run` prints the result table instead of writing to the target sheet.
- `--limit N` processes only the first `N` data rows (for testing).
- `--verbose` enables debug logging.
- `--force` ignores the cache and asks the LLM to revalidate every row.
- `--checkdate` refreshes any rows that either lack a "Check date" value or have a timestamp from a previous week, forcing a single LLM call for stale rows and unlimited calls for missing dates.

By default the tool stores cached responses in an SQLite file next to the configuration. Set `cache_path` in the YAML to relocate the cache or remove the file to reset the stored answers.

## Remote Deployment
1. **Provision Python and credentials.** Ensure the remote host has Python 3.10+ and copy the service account JSON plus your `config.yaml` (or fetch them from a secrets manager at runtime).
2. **Clone the repository.**
   ```bash
   ssh user@server
   git clone https://github.com/your-org/StatusValidator.git
   cd StatusValidator
   ```
3. **Create a virtual environment and install dependencies.**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -e .
   ```
4. **Configure secrets.** Place environment variables (for example `OPENAI_API_KEY`) in a `.env` file in the project root or alongside the YAML config so they load automatically. Keep `service-account.json` outside of version control and update `config.yaml` to point to the deployed path.
5. **Dry-run validation.** Confirm connectivity and credentials before automating:
   ```bash
   venv/bin/status-validator --config /path/to/config.yaml --dry-run --limit 5
   ```

## Scheduled Runs
Two common options are shown below; pick the one that matches your infrastructure standards. In both cases wrap the CLI call in a tiny shell script that activates the virtual environment and loads `.env`.

Create `run_status_validator.sh` in the project root:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
set -a; [ -f .env ] && source .env; set +a
source venv/bin/activate
status-validator --config /path/to/config.yaml --checkdate >> logs/status-validator.log 2>&1
```

Make it executable: `chmod +x run_status_validator.sh` and ensure the `logs/` directory exists.

### Cron
Edit the crontab (`crontab -e`) and schedule the script, for example every weekday at 07:00 server time:

```
0 7 * * 1-5 /home/user/StatusValidator/run_status_validator.sh
```

### systemd timer
Create `/etc/systemd/system/status-validator.service`:

```
[Unit]
Description=Status Validator
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/home/user/StatusValidator
ExecStart=/home/user/StatusValidator/run_status_validator.sh
```

Create `/etc/systemd/system/status-validator.timer`:

```
[Unit]
Description=Run Status Validator on schedule

[Timer]
OnCalendar=Mon..Fri 07:00
Persistent=true

[Install]
WantedBy=timers.target
```

Reload systemd (`sudo systemctl daemon-reload`), enable and start the timer:

```bash
sudo systemctl enable --now status-validator.timer
```

## Output
Every processed row produces the following fields in the result sheet:
- Row number and direct link to the original entry.
- Project name taken from the column referenced by `columns.identifier` (if configured) and rendered as a hyperlink to the original row.
- Original status, comment, and completion date.
- Validation flag (`YES`/`NO`).
- Bullet list with remarks from the LLM.
- Full rewrite suggestion that complies with the rulebook.
- Raw JSON returned by the LLM for traceability.
- Automatic check timestamp in the "Check date" column and the LLM identifier written to the "Model" column.

## Local Validation
To check the project compiles:

```bash
python -m compileall status_validator
```

Additional tests can be added under `tests/` with `pytest` if needed.
