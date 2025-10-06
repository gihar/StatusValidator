#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
set -a; [ -f .env ] && source .env; set +a
source venv/bin/activate
python -m status_validator.main --config config.yaml --limit 250 --checkdate >> logs/status-validator.log 2>&1
