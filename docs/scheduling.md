# Выбор worker и автоматический перенос CI

Каждый host имеет отдельный `worker_id`. Запрос остаётся immutable и адресуется
одному worker; перенос создаёт новый job ID для той же публикации, SHA, ветки,
profile, workflow/event/job/inputs и GitHub namespace. `retry_of` сохраняет цепочку
попыток. Изменять worker или revision в опубликованном request нельзя.

## Выбор до отправки

```bash
gdi ci workers drive --json
gdi push drive --ci --json
gdi ci submit drive --publication PUBLICATION_ID --json
```

Без `--worker` клиент выбирает доступный совместимый host. Явный `--worker ID`
сохраняет прежний режим: запрос адресуется этому ID без скрытого fallback.
Для автоматического выбора с ограничениями используйте `--policy PATH`.
`examples/ci-portable.json` — пример **повторяемого** CI для ubuntu-latest;
его нельзя применять к публикации релизов или работе с устройствами без отдельного решения.

Capabilities подтверждают profile revision, поддержку отмены, метки `labels` и
доступные workflow `platforms`. Свежий worker status с `registry_version: 1`
подтверждает эти же revisions/метки/platforms, `busy` и `queue_length`.
Статус обновляется при polling в простое и при публикации heartbeat выполняемого
job. Старые capabilities и старый READY/RUNNING не подтверждают доступность.
`ci workers` показывает причину исключения: устаревший/отсутствующий heartbeat,
несовпадающая revision, отсутствующий profile, метки/platforms или поддержка отмены.

Каждый элемент `ci workers` также содержит `heartbeat` с `state`, timestamp,
возрастом и порогом `worker_fresh_seconds`, даже если capabilities несовместимы.
`reason_code` даёт причину исключения, `reason` — пояснение; доступный worker имеет
оба поля null. Для проверки конкретного host смотрите следующие данные:

| Причина | Данные и дальнейшая проверка |
| --- | --- |
| `capabilities_missing`, `capabilities_invalid` | Проверить worker config и `gdi worker check --runtime` на host |
| `profile_missing`, `repository_missing` | Сверить `requested_profile`, `available_profiles` и регистрацию репозитория |
| `registry_status_missing` | Отсутствующий/старый формат status или чужой worker ID; обновить и запустить worker |
| `registry_status_invalid`, `worker_not_ready` | Некорректные busy/queue либо `worker_state`; проверить журнал worker |
| `revision_mismatch` | Сверить `advertised_revisions` и `heartbeat_revisions`, обновить снимок; при изменении среды перезапустить worker |
| `heartbeat_stale`, `heartbeat_missing`, `heartbeat_invalid` | Проверить возраст/порог, службу, сеть и доставку статуса; остановка CI не доказана |
| `heartbeat_clock_skew` | Синхронизировать часы host/клиента; проверить `clock_ahead_seconds` |
| `labels_missing`, `platforms_missing` | Конкретные `missing_labels`/`missing_platforms`; выбрать совместимый host или настроить его окружение |
| `labels_mismatch`, `platforms_mismatch` | Capabilities и status расходятся; получить свежие данные и проверить конфигурацию host |

Порог worker heartbeat по умолчанию 300 секунд, job heartbeat в `ci status` —
600 секунд. Первый определяет пригодность для нового назначения, второй служит
диагностикой уже выполняемого задания. `ci workers` не подключается к Docker
удалённого host: состояние act/Docker/images проверяется на самом host через
`gdi worker check --runtime`, а revision привязывает опубликованные execution settings.

Публичные метки можно задать в worker.json, например `"labels": ["linux", "lab-board"]`.
После изменения перезапустите worker: непустые метки входят в execution revision.
Имена platforms берутся из `platforms` worker config. Для workflow с matrix или
выражениями укажите необходимые platforms/метки в политике; gdi не выводит их
автоматически из произвольного YAML. Значения secrets не публикуются.

`queue_length` — известные локальному ledger ожидающие задания; новые сообщения
inbox, ещё не прочитанные занятым host, в эту оценку не входят. Выбор не резервирует
свободный слот: параллельные клиенты могут адресовать разные jobs одному worker,
который исполнит их последовательно.

| `selection` | Поведение |
| --- | --- |
| `load` (default) | Свободный host, затем меньшая очередь; round robin при равенстве |
| `round_robin` | Следующий доступный совместимый ID по алфавиту, с переходом в начало |
| `preferred` | `preferred_worker`, если доступен; иначе выбор по загрузке |
| `current` | То же, но `preferred_worker` должен быть фактическим ID текущего host из его config |

`pinned_worker` запрещает скрытый fallback на другой host и имеет приоритет над
selection. Для переносимого CI текущий компьютер владельца не обязан быть исполнителем.
Round robin хранит последний выбор локально; это не глобальный счётчик Drive.
Повтор автоматического submit с той же публикацией/selector/политикой сохраняет
выбранные ID/revision и bytes в `.gdi/ci/<repository-id>/dispatch/`, включая сбой
доставки. Для нового исполнения завершённого job используйте явный retry.
Изменение окружения после сохранения плана вызывает отказ, а не незаметную замену запроса.

## Таймауты и повторяемость

Политика JSON допускает следующие поля; пропущенные получают defaults:

| Поле | Default и назначение |
| --- | --- |
| `policy_version` | `1` |
| `queue_timeout_seconds` | `900`: ожидание с момента created_at до первого claim/status |
| `heartbeat_timeout_seconds` | `600`: отсутствие свежего job heartbeat после начала |
| `worker_fresh_seconds` | `300`: допустимый возраст heartbeat кандидата |
| `max_attempts` | `3`, включая исходную попытку и её проверенных предшественников |
| `backoff_seconds` | `30`, удваивается для каждой следующей замены |
| `retry_mode` | `confirmed_stop`; также `repeatable`, `never` |
| `selection` | `load`; остальные режимы описаны выше |
| `preferred_worker`, `pinned_worker` | `null` |
| `required_labels`, `required_platforms` | `[]`; все перечисленные должны быть у host |

Лог может молчать при работающем процессе. Только claim/status выбирает режим
heartbeat timeout; строки логов в измерении liveness не участвуют. У старого claim
нет timestamp, поэтому при первом наблюдении даётся полный heartbeat timeout.
Сохранённый claim не превращается обратно в QUEUED после исчезновения файла.
Часы host и клиента должны быть синхронизированы; заметный future timestamp или
движение heartbeat назад вызывает ошибку. Краткие задержки сети не повод для переноса.

По таймауту supervisor сначала проверяет полный terminal result. Уже проверенный
PASS/FAIL/другой terminal state не отменяется задним числом. Если результата нет,
сохраняется и доставляется проверяемый `cancel.json`. Ошибка чтения Drive,
исчезновение request/папки и удаление inbox не доказывают отмену или остановку.
Частичная доставка результата также не считается проверенным завершением.

- `repeatable`: после проверенной доставки отмены и backoff можно назначить B,
  даже если A без сети продолжает CI. Используйте только для допускающих такое
  повторение заданий. Исходный A при возвращении учитывает отмену и поздний PASS
  блокируется существующим механизмом cancellation fence.
- `confirmed_stop`: новый запуск ждёт полного проверенного CANCELLED исходного
  worker. Выключенный A не сможет подтвердить остановку до возвращения.
- `never`: доставить отмену, сохранить историю и не повторять исполнение.

Для устройств, внешних записей и других побочных эффектов задайте отдельную
политику: `retry_mode: never` либо явно разрешённый повтор после подтверждённой
остановки, `pinned_worker` и необходимые метки. Остановка процесса сама по себе
не отменяет уже произведённых внешних изменений. Закреплённый job может быть
повторён на том же host только после проверенного CANCELLED, даже в repeatable.

## CLI supervisor

```bash
gdi ci supervise drive JOB_ID --policy examples/ci-portable.json --json
gdi ci supervise drive JOB_ID --policy examples/ci-portable.json --watch --json
```

Без `--watch` команда выполняет один шаг. С `--watch` она следует за всей цепочкой,
выбирает worker без ручного назначения и завершает ожидание на проверенном результате
или лимите попыток. Интервал не меньше 30 секунд; default 30. `--timeout` ограничивает
длительность этого вызова (default 3600), не означает отмену удалённого job.
Ctrl+C останавливает supervisor; уже доставленная отмена и история сохраняются.
Повторяйте команду с **исходным JOB_ID** и той же политикой.

Ответы: MONITORING, BACKOFF, WAITING_FOR_STOP, NO_COMPATIBLE_WORKER, REASSIGNED,
ATTEMPTS_EXHAUSTED либо terminal state с `result.verified: true`. REASSIGNED не
означает, что B уже начал выполнение. Финальный PASS принимается только после
проверки всех artifacts/chunks. Watch timeout возвращает код 124, terminal PASS — 0,
другой terminal result/исчерпание попыток — 1.

Сессия хранится в `.gdi/ci/<repository-id>/supervision/JOB_ID/`; её policy и история
зафиксированы. Не удаляйте state/outbox/запросы/отмены для сброса лимита или повторного
запуска. Автоматическая очистка данных остаётся отдельной retention-задачей.

## Агент через Drive-коннектор

Вся подготовка и проверка выполняется `gdi agent ci`; коннектор только передаёт
точные файлы, возвращённые tools. Не составляйте и не удаляйте протокольные файлы вручную.
Получите свежий полный snapshot проекта, включая request/ready/status/claim/cancel/
successor активного job. Включите предшественников при начальном восстановлении
цепочки retry и полные artifacts/chunks любого принимаемого результата.

Отдельный `workers.json` описывает полный listing общей `ci/workers`, все страницы
каждой worker-папки и реальные downloads capabilities/status:

```json
{
  "workers_version": 1,
  "folder_id": "WORKERS_FOLDER_ID",
  "pages": [{"page_token": null, "next_page_token": null, "entries": [
    {"id": "HOST_FOLDER_ID", "name": "host-a", "is_dir": true, "bytes": null}
  ]}],
  "workers": [{
    "worker_id": "host-a", "folder_id": "HOST_FOLDER_ID",
    "pages": [{"page_token": null, "next_page_token": null, "entries": [
      {"id": "CAPS_FILE_ID", "name": "capabilities.json", "is_dir": false, "bytes": 123},
      {"id": "STATUS_FILE_ID", "name": "status.json", "is_dir": false, "bytes": 234}
    ]}],
    "files": [
      {"file_id": "CAPS_FILE_ID", "local_path": "host-a/capabilities.json"},
      {"file_id": "STATUS_FILE_ID", "local_path": "host-a/status.json"}
    ]
  }]
}
```

IDs и bytes замените фактическими. Правила полной пагинации, уникальности IDs/имён
и файлов без symlinks совпадают с project snapshot. Включите каждый worker из
listing, даже если у него отсутствует status; присутствующий файл требует download.
Свежесть, принадлежность worker registry выбранному общему root и полноту получения
с Drive обеспечивает агент через коннектор. Перекачивание старого статуса не обновляет
его `updated_at`.

```bash
python3 -m gdi agent ci workers --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --workers "$EXCHANGE_DIR/workers.json" --policy "$EXCHANGE_DIR/policy.json"
python3 -m gdi agent ci prepare --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --publication PUBLICATION_ID --workers "$EXCHANGE_DIR/workers.json" \
  --policy "$EXCHANGE_DIR/policy.json" --output "$EXCHANGE_DIR/ci-plan"
python3 -m gdi agent ci supervise --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --workers "$EXCHANGE_DIR/workers.json" --policy "$EXCHANGE_DIR/policy.json" \
  --job ORIGINAL_JOB_ID --output "$EXCHANGE_DIR/supervisor"
```

Prepare без `--worker` выбирает host и сохраняет его в плане. Дальше используйте
обычные check/accept с capabilities выбранного host и проверкой request/ready/inbox.
Supervisor выдаёт последовательные действия:

1. CANCEL_PREPARED: загрузить `plan.files.cancel`, проверить доставку существующим
   `agent ci cancel-check` и обновлённым snapshot. Подтверждение доставки не означает остановку.
2. BACKOFF/WAITING_FOR_STOP/NO_COMPATIBLE_WORKER: сохранить сессию, получить свежие
   данные на следующем цикле. Worker ID руками не назначается.
3. SUCCESSOR_PREPARED: загрузить только точный `files.successor`; скачать обратно
   в новый snapshot. Этот файл фиксирует новую попытку до загрузки её inbox.
4. RETRY_PREPARED: передать `plan.files.request`, затем ready; обновить snapshot,
   повторить supervise/check. Inbox разрешён только после `safe_to_upload_inbox: true`.
5. Передать inbox, сохранить полный inbox-proof как при обычном accept; выполнить
   supervise с `--inbox-proof PATH` либо `agent ci accept --plan PATH` для выданного
   плана и затем повторить supervise. Сессия переключается на новый job после accept.
6. Следующие снимки должны содержать новый активный job. Повтор supervise использует
   исходный job ID и тот же output. Проверенный terminal result завершает цикл.

Это шаги автономного агента через tools, не скрытый фоновый процесс. Агент выдерживает
паузы минимум 30 секунд; фоновые уведомления требуют реально запущенного внешнего механизма.

## Конкуренция и границы

Для одного корневого job используйте один coordinator и сохраняемый каталог сессии.
Локальный flock предотвращает параллельные вызовы этой сессии. До inbox публикуется
immutable `ci/jobs/OLD_JOB_ID/successor.json`, связанный с checksum исходного request,
политикой и точным новым request. ID автоматической замены детерминирован для старого
job и политики; повтор доставки сохраняет bytes. Клиент и worker сверяют successor;
конфликтующие proposals/дубликаты Drive вызывают отказ, а не выбор победителя.

Обычная запись Drive не является CAS или распределённым lock. Несколько независимых
coordinators с разными сессиями могут конфликтовать; exactly-once и глобальная
резервация слотов не обещаются. Обнаруженный конфликт требует остановки конкурирующих
coordinators и проверки истории. Старые workers без registry heartbeat не подходят
для автоматического выбора; явный режим остаётся доступен. Обновите все участвующие
workers перед использованием supervisor.
