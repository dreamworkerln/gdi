# CI protocol v1/v2 — gdi 0.3

Отдельный от [Git protocol v2](protocol.md) namespace `ci/`. Один назначенный
worker на общий root; legacy v1 — на repository remote. Все job IDs/run IDs — 32 lowercase hex, Git SHA — 40,
publication IDs/profile revisions/SHA256 — 64. Worker/profile IDs: ASCII letters,
digits, `_`, `-`, до 64 символов. JSON UTF-8, canonical encoding как у Git metadata;
duplicate keys и metadata больше 1 MiB отвергаются. Неизвестные request/result fields
отвергаются. Доступ участников к Drive доверенный, подписи/авторство не проверяются.

## Общая inbox и request v2

Новый worker config version 2 публикует capabilities **в общем root**:
`{ci_version:1, inbox_version:1, worker_id, profiles:{full:revision}}`.
Списка репозиториев нет. Revision закрепляет общие параметры исполнения.
Пути проектов берутся из immutable notifications общей `inbox`; schema, checksum,
маршрутизация и подтверждение описаны в [inbox.md](inbox.md).

Request v2 содержит те же identity fields, что v1 ниже, но `ci_version: 2` и
дополнительное поле `workflow` с точными ключами:
`{path:".github/workflows", event:"push", job:"", inputs:{}}`.
Selector не содержит executable/argv, host paths или secrets. Он выбирает YAML
**внутри checkout закреплённого commit**; исполняет его act.

Порядок публикации v2: durable outbox → request → request.ready → shared inbox.
Ready marker содержит ci_version запроса. Worker проверяет hash request из
уведомления, ready, repository ID, branch/head/publication и worker/job IDs.
Result сохраняет ci_version исходного request и связывает полный selector через
request_sha256; остальные поля и проверки результата ниже общие для v1/v2.
Advisory status/events пока используют status schema version 1.
После read-back результата удаляется конкретное уведомление inbox; `ci/queue`
внутри проекта для v2 не создаётся и не опрашивается.

## Request v1 (legacy)

Путь: `ci/jobs/<job-id>/request.json`. Точный набор полей:

```json
{
  "ci_version": 1,
  "job_id": "11111111111111111111111111111111",
  "repository_id": "0123456789abcdef0123456789abcdef",
  "ref": "refs/heads/dev",
  "head": "0123456789abcdef0123456789abcdef01234567",
  "publication_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "worker_id": "user-host",
  "profile_id": "full",
  "profile_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "created_at": "2026-10-09T00:00:00+00:00",
  "retry_of": null
}
```

Новый повтор исполнения — новый ID с `retry_of`. Commands/env/host paths в request
отсутствуют. Путь определяется проверенным job ID. Publication/ref/head должны
совпасть с проверенной Git metadata. Worker исполняет только локально зарегистрированный
профиль с совпавшей revision; иначе terminal REJECTED.

Capabilities: `ci/workers/<worker-id>/capabilities.json`:
`{ci_version:1, worker_id, repositories:{repository_id:{profile_id:revision}}}`.
Snapshot mutable. Profile revision вычисляется из нормализованного config; сам config
и его env values на Drive не публикуются.

## Commit входного задания v1 (legacy)

`ci/queue/<job-id>.json` и `ci/jobs/<job-id>/request.ready` содержат одинаковые поля:
`ci_version:1`, `job_id`, `request_sha256` от точных bytes request.

Порядок: local outbox fsync → request upload → queue pointer upload → ready upload.
Worker проверяет обе ссылки и request. Отсутствие ready означает незавершённую
отправку, не FAIL. Queue index просматривается отдельно от архива jobs.

Claim `worker.running.json` immutable: `ci_version`, `job_id`, `run_id`, `worker_id`,
`request_sha256`. Ledger сохраняется до claim upload. Claim чужого run не перехватывается.
Это не CAS, один worker назначается оператором.

## Status, events и streaming

Job `status.json` и worker `status.json` mutable advisory. Поля job status:
`ci_version`, `job_id`, `run_id`, `worker_id`, `request_sha256`, `state`, `stage`
(имя либо null), `sequence` (возрастающий integer), `updated_at` (UTC ISO timestamp).
Events сохраняют такой snapshot при смене state/stage в `events/<sequence:08d>.json`;
heartbeat увеличивает sequence, но не создаёт event, поэтому gaps в **events** допустимы.

Консоль — объединённые stdout+stderr, bytes без преобразований. Для act это JSON lines;
в результате одна blocking-стадия `github-actions`, детализация jobs/steps — в log. Publisher пишет
`log-chunks/<sequence:08d>-<sha256>.bin`, начиная с 1 без gaps. Один chunk не больше
256 KiB; при очередном upload cycle может быть меньше. Клиент проверяет каждый hash,
сортировку, непрерывность и отсутствие дубликатов. При терминальном результате
конкатенация всех chunks должна точно совпасть по bytes/SHA256 с `build.log`.

Worker закрывает console file и fsync до фиксации результата. Mutable status с PASS
сам по себе никогда не является доказательством успешной проверки.

## Result и artifacts

Порядок публикации: chunks/events → объявленные artifacts → `build.log` →
`final-status.json` → `result.json` последним. Локальный result сохраняется **до**
финального upload, сетевой retry не выполняет CI повторно.

Result содержит точные identity fields из request: `ci_version`, `job_id`,
`repository_id`, `ref`, `head`, `publication_id`, `worker_id`, `profile_id`,
`profile_revision`; дополнительно:

| Поле | Содержание |
| --- | --- |
| `request_sha256`, `run_id` | Связь с request и попыткой worker |
| `state`, `exit_code`, `failed_stage` | Terminal state, integer code, имя стадии либо null |
| `stages` | Упорядоченные `{name, blocking, state, exit_code}` фактически выполненных стадий |
| `warnings`, `detail` | Warnings non-blocking стадий и текст диагностической ошибки |
| `started_at`, `finished_at`, `duration_seconds` | Время и длительность для диагностики |
| `artifacts` | Descriptors всех опубликованных files, включая log/final status |

Descriptor: `{path, bytes, sha256, complete:true}`. Разрешённые относительные paths:
`build.log`, `final-status.json`, `artifacts/<safe-name>`. Список без дубликатов.
Log и final status обязательны даже при ошибке подготовки. Legacy profile whitelist
ограничивает artifacts, required missing или symlink outside даёт ERROR. Для act
artifact store архивируется в `artifacts/workflow-artifacts.zip`; symlinks запрещены.
Exit 0 без завершённого успешного job не принимается как PASS.

| State | Смысл |
| --- | --- |
| PASS | Все обязательные стадии выполнены и прошли, источники/HEAD соответствуют request, artifacts собраны |
| FAIL | Blocking команда вернула ненулевой код |
| ERROR | Ошибка подготовки/окружения/консоли/artifacts либо изменение исходников/HEAD |
| TIMEOUT | Host лимит исполнения превышен, группа процессов остановлена |
| INTERRUPTED | После сбоя исход исполнения неопределён; требуется явный retry |
| REJECTED | Профиль отсутствует или revision не совпадает |

Агент принимает terminal result только после проверок identity/hash/size всех
artifacts, final status и полного streaming log. Клиентский `verified` не является
полем remote result. Upload pending — локальное состояние доставки, не CI FAIL.

## Recovery и GC

Ledger PUBLISHED фиксируется после read-back проверки result/artifacts, затем
удаляется только `ci/queue/<job-id>.json`. Crash между этими действиями исправляется
при discovery, без исполнения. Request/ready/claim/chunks/result остаются в архиве.

При RUNNING/FINALIZING без durable result нет автоматического rerun: INTERRUPTED.
Сохранившийся result допубликовывается независимо от прежнего ledger state. Повреждение
локального закрытого artifact/chunk блокирует доставку и не вызывает повтор CI.

Apply-GC bundles отказывает при queue pointer или неполном/неterminal job. Completed
CI artifacts не удаляются. Все clients и worker должны быть остановлены; snapshot
checks не заменяют распределённый lock. Retention CI namespace пока не реализован.
