# Руководство по внедрению многопоточности

Этот документ содержит пошаговые инструкции по внедрению многопоточной обработки запросов к LLM в проект StatusValidator.

## Предварительные требования

- Python 3.10+
- Все зависимости установлены (`pip install -e .`)
- Рабочая конфигурация с доступом к OpenAI API
- Бэкап базы данных SQLite кеша (опционально)

## Этап 1: Обновление конфигурации

### 1.1. Обновить `status_validator/config.py`

Добавить поле `max_workers` в `LLMConfig`:

```python
class LLMConfig(BaseModel):
    max_retries: int = Field(
        3,
        ge=1,
        description="Number of attempts per provider before switching to the next one",
    )
    max_workers: int = Field(  # НОВОЕ ПОЛЕ
        1,
        ge=1,
        le=20,
        description="Number of parallel threads for LLM requests (1 = sequential)",
    )
    providers: dict[int, LLMProviderConfig] = Field(
        ...,
        description="Mapping of priority -> provider configuration",
    )
    # ... остальные поля
```

### 1.2. Обновить `config.yaml`

Добавить параметр `max_workers` в секцию `llm`:

```yaml
llm:
  max_retries: 3
  max_workers: 5  # НОВЫЙ ПАРАМЕТР: 1 = последовательно, 5 = рекомендуемое значение
  providers:
    1:
      name: primary
      model_env: OPENAI_MODEL_1
      api_key_env: OPENAI_API_KEY_1
      temperature: 0.0
      max_output_tokens: 10000
```

### 1.3. Обновить `config.example.yaml`

Добавить комментарии и пример:

```yaml
llm:
  max_retries: 3
  max_workers: 5  # Number of parallel threads (1-10 recommended, 1 = sequential)
                  # Higher values increase throughput but may hit rate limits
                  # Start with 5 and adjust based on your API tier
  providers:
    1:
      name: primary
      model_env: OPENAI_MODEL_1
      api_key_env: OPENAI_API_KEY_1
      temperature: 0.0
      max_output_tokens: 10000
```

## Этап 2: Thread-safe кеш

### 2.1. Обновить `status_validator/cache.py`

**Добавить импорт в начало файла:**

```python
import threading
```

**Обновить `CacheStore.__init__`:**

```python
def __init__(self, path: Path) -> None:
    self._path = path
    self._lock = threading.Lock()  # НОВОЕ: блокировка для thread-safety
    
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    
    # check_same_thread=False позволяет использовать соединение из разных потоков
    self._conn = sqlite3.connect(str(path), check_same_thread=False)  # ИЗМЕНЕНО
    self._conn.row_factory = sqlite3.Row
    self._ensure_schema()
```

**Обновить метод `get_payload`:**

```python
def get_payload(
    self,
    *,
    source_id: str,
    sheet_name: str,
    row_number: int,
    status_text: str,
    comment_hash: str,
) -> Optional[Dict[str, Any]]:
    """Fetch cached payload if status and comment hash match."""
    
    with self._lock:  # НОВОЕ: защита доступа к БД
        cursor = self._conn.execute(
            """
            SELECT payload_json
            FROM llm_cache
            WHERE source_id = ?
              AND sheet_name = ?
              AND row_number = ?
              AND status_text = ?
              AND comment_hash = ?
            """,
            (source_id, sheet_name, row_number, status_text, comment_hash),
        )
        row = cursor.fetchone()
    
    if not row:
        return None
    
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError:
        LOGGER.warning(
            "Cached payload for row %s is not valid JSON; ignoring",
            row_number,
        )
        return None
```

**Обновить метод `store_payload`:**

```python
def store_payload(
    self,
    *,
    source_id: str,
    sheet_name: str,
    row_number: int,
    status_text: str,
    comment_hash: str,
    payload: Dict[str, Any],
) -> None:
    """Persist payload for the given row, overriding previous entry."""
    
    payload_json = json.dumps(payload, ensure_ascii=False)
    timestamp = time.time()
    
    with self._lock:  # НОВОЕ: защита доступа к БД
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO llm_cache (
                    source_id,
                    sheet_name,
                    row_number,
                    status_text,
                    comment_hash,
                    payload_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, sheet_name, row_number)
                DO UPDATE SET
                    status_text = excluded.status_text,
                    comment_hash = excluded.comment_hash,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    sheet_name,
                    row_number,
                    status_text,
                    comment_hash,
                    payload_json,
                    timestamp,
                ),
            )
```

**Обновить метод `close`:**

```python
def close(self) -> None:
    with self._lock:  # НОВОЕ: защита при закрытии
        self._conn.close()
```

## Этап 3: Параллельная обработка в pipeline

### 3.1. Создать новый модуль `status_validator/parallel.py`

Скопировать содержимое из `MULTITHREADING_PROTOTYPE.py` в новый файл:

```bash
cp MULTITHREADING_PROTOTYPE.py status_validator/parallel.py
```

Обновить импорты в начале файла:

```python
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

from .config import AppConfig
from .google_sheets import GoogleSheetsClient
from .llm_client import LLMClient
from .models import StatusEntry, ValidationResult
from .pipeline import validate_entry

LOGGER = logging.getLogger(__name__)
```

### 3.2. Обновить `status_validator/main.py`

**Добавить импорт:**

```python
from .parallel import validate_batch_parallel
```

**Заменить цикл обработки батча (строки 284-397):**

Было:
```python
for entry in batch:
    # ... логика валидации одной записи
    result = validate_entry(entry, config, sheets_client, llm_client)
    results.append(result)
```

Стало:
```python
# Определяем, использовать ли параллельную обработку
max_workers = config.llm.max_workers if hasattr(config.llm, 'max_workers') else 1

if max_workers > 1:
    # Параллельная обработка батча
    batch_successful, batch_failed = validate_batch_parallel(
        batch,
        config,
        sheets_client,
        llm_client,
        max_workers=max_workers,
    )
    
    # Обработка успешных результатов
    for entry, result in batch_successful:
        # ... существующая логика обработки результата
        processed_entries.append(entry)
        results.append(result)
        # и т.д.
    
    # Добавление неудачных в список
    failed_entries.extend(batch_failed)
else:
    # Последовательная обработка (текущая логика)
    for entry in batch:
        # ... существующая логика
```

## Этап 4: Обработка ошибок rate limiting

### 4.1. Обновить `status_validator/llm_client.py`

Добавить обработку 429 ошибок в методе `_generate_with_provider`:

```python
try:
    response = provider.client.chat.completions.create(**api_params)
except APIError as exc:
    error_message = str(exc).lower()
    
    # Проверка на rate limiting
    if "429" in error_message or "rate limit" in error_message:
        if attempt < max_attempts:
            retry_delay = 2 ** attempt  # exponential backoff
            LOGGER.warning(
                "Rate limit hit, retrying in %d seconds (attempt %d/%d)",
                retry_delay,
                attempt,
                max_attempts,
            )
            time.sleep(retry_delay)
            continue
    
    raise RuntimeError(f"LLM request failed: {exc}") from exc
```

## Этап 5: Тестирование

### 5.1. Запуск бенчмарка

```bash
python benchmark_multithreading.py --config config.yaml --limit 20 --workers 5
```

Ожидаемые результаты:
- Прирост производительности 3-5x
- Все записи обработаны корректно
- Нет ошибок в логах

### 5.2. Тестирование с разным количеством потоков

```bash
# Последовательная обработка (базовый уровень)
# Обновить config.yaml: max_workers: 1
python -m status_validator.main --config config.yaml --limit 10 --dry-run

# 3 потока
# Обновить config.yaml: max_workers: 3
python -m status_validator.main --config config.yaml --limit 10 --dry-run

# 5 потоков (рекомендуется)
# Обновить config.yaml: max_workers: 5
python -m status_validator.main --config config.yaml --limit 10 --dry-run

# 10 потоков (может привести к rate limiting)
# Обновить config.yaml: max_workers: 10
python -m status_validator.main --config config.yaml --limit 10 --dry-run
```

### 5.3. Проверка корректности

1. Сравнить результаты с `--dry-run`
2. Проверить, что все записи обработаны
3. Проверить качество ответов LLM
4. Проверить кеш: повторный запуск должен быть быстрее

### 5.4. Нагрузочное тестирование

```bash
# Обработка большого количества записей
python -m status_validator.main --config config.yaml --limit 100 --dry-run

# Мониторинг логов на наличие:
# - Rate limiting errors (429)
# - Timeout errors
# - Prompt cache hit rate
```

## Этап 6: Мониторинг и настройка

### 6.1. Метрики для отслеживания

Добавить логирование в `status_validator/parallel.py`:

```python
# В конце validate_batch_parallel
LOGGER.info(
    "Batch stats: %.2f entries/sec, %.2f%% success rate",
    len(entries) / elapsed_time if elapsed_time > 0 else 0,
    len(successful_results) / len(entries) * 100 if entries else 0,
)
```

### 6.2. Оптимальные настройки

Начальная конфигурация для разных API тарифов:

| Тариф OpenAI | RPM лимит | Рекомендуемые workers |
|--------------|-----------|----------------------|
| Free         | 20        | 1-2                  |
| Tier 1       | 500       | 3-5                  |
| Tier 2       | 3500      | 5-10                 |
| Tier 3+      | 10000+    | 10-15                |

### 6.3. Признаки необходимости уменьшения workers

- Частые ошибки 429 (rate limiting)
- Снижение prompt cache hit rate
- Увеличение количества timeout ошибок
- Снижение общей производительности

### 6.4. Признаки возможности увеличения workers

- 0 ошибок rate limiting
- Высокий hit rate кеша (>80%)
- Стабильное время ответа API
- Использование менее 50% доступного RPM

## Этап 7: Обновление документации

### 7.1. Обновить `README.md`

Добавить секцию о параллельной обработке:

```markdown
### Parallel Processing

StatusValidator supports parallel LLM requests to significantly improve throughput:

- **Sequential mode** (`max_workers: 1`): Process one entry at a time
- **Parallel mode** (`max_workers: 5`): Process 5 entries simultaneously (recommended)

Configure in `config.yaml`:

​```yaml
llm:
  max_workers: 5  # 1-10 recommended, adjust based on API tier
​```

**Performance gains:**
- 3-5x faster for batches of 10+ entries
- Best with higher OpenAI API tier (Tier 2+)
- Automatically handles rate limiting with exponential backoff
```

### 7.2. Обновить `config.example.yaml`

Убедиться, что пример содержит все новые параметры с комментариями.

## Откат изменений

Если возникли проблемы, можно вернуться к последовательной обработке:

1. **Быстрый откат через конфигурацию:**
   ```yaml
   llm:
     max_workers: 1  # Отключить параллелизм
   ```

2. **Полный откат через git:**
   ```bash
   git checkout -- status_validator/cache.py
   git checkout -- status_validator/main.py
   # Удалить новые файлы
   rm status_validator/parallel.py
   ```

## Контрольный список

- [ ] Обновлён `config.py` с полем `max_workers`
- [ ] Обновлён `config.yaml` с настройкой `max_workers: 5`
- [ ] Обновлён `cache.py` для thread-safety (lock + check_same_thread=False)
- [ ] Создан модуль `parallel.py` с функциями параллельной обработки
- [ ] Обновлён `main.py` для использования параллельной обработки
- [ ] Добавлена обработка rate limiting в `llm_client.py`
- [ ] Запущен бенчмарк и получен прирост производительности
- [ ] Протестирована корректность результатов
- [ ] Проверен prompt cache hit rate
- [ ] Обновлена документация (README.md, config.example.yaml)
- [ ] Создан git commit с изменениями

## Дополнительные ресурсы

- `MULTITHREADING_ANALYSIS.md` - детальный анализ архитектуры
- `MULTITHREADING_PROTOTYPE.py` - примеры функций
- `CACHE_THREAD_SAFE_PATCH.py` - патч для кеша
- `benchmark_multithreading.py` - скрипт бенчмарка
- [OpenAI Rate Limits](https://platform.openai.com/docs/guides/rate-limits)
- [Python ThreadPoolExecutor](https://docs.python.org/3/library/concurrent.futures.html)

## Поддержка

При возникновении проблем:

1. Проверить логи на наличие ошибок
2. Уменьшить `max_workers` до 1-2
3. Проверить rate limits API
4. Создать issue с логами и конфигурацией

