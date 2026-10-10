# CI protocol v1/v2 — gdi 0.3

Отдельный от [Git protocol v3](protocol.md) namespace `ci/`. Один назначенный
worker на запрос; несколько workers общего root используют разные worker_id.
Legacy v1 — на repository remote. Все job IDs/run IDs — 32 lowercase hex, Git SHA — 40,
publication IDs/profile revisions/SHA256 — 64. Worker/profile IDs: ASCII letters,
digits, `_`, `-`, до 64 символов. JSON UTF-8, canonical encoding как у Git metadata;
duplicate keys и metadata больше 1 MiB отвергаются. Неизвестные request/result fields
отвергаются. Доступ участников к Drive доверенный, подписи/авторство не проверяются.

## Общая inbox и request v2

Новый worker config version 2 публикует capabilities **в общем root**:
`{ci_version:1, inbox_version:1, cancel_version:1, worker_id, profiles:{full:revision}}`.
Списка репозиториев нет. Revision закрепляет общие параметры и фактическое окружение
исполнения: SHA256/version act, host actrc hashes, Docker daemon/version и local base image IDs.
Пути проектов берутся из immutable notifications общей `inbox`; schema, checksum,
маршрутизация и подтверждение описаны в [inbox.md](inbox.md).

Request v2 содержит те же identity fields, что v1 ниже, но `ci_version: 2` и
дополнительное поле `workflow` с точными ключами:
`{path:".github/workflows", event:"push", job:"", inputs:{}}`.
Selector не содержит executable/argv, host paths или secrets. Он выбирает YAML
**внутри checkout закреплённого commit**; исполняет его act.
Необязательное поле `github_repository` содержит только `owner/repository`, полученное
из GitHub origin клиента. Старые запросы v2 без этого поля сохраняют совместимость.
Namespace входит в bytes/hash запроса и сохраняется при retry; произвольный origin URL
и credentials в request не копируются.

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
`{ci_version:1, cancel_version:1, worker_id, repositories:{repository_id:{profile_id:revision}}}`.
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
Это не CAS: worker закреплён в request, claims разных run не перехватываются.

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
Фактическое окружение global runner включается в `artifacts/environment.json`.
Exit 0 без завершённого успешного job не принимается как PASS.

| State | Смысл |
| --- | --- |
| PASS | Обязательные стадии прошли; act начал с checkout выбранного SHA и завершил успешный job; host checkout/HEAD проверены, artifacts собраны |
| FAIL | Blocking команда вернула ненулевой код |
| ERROR | Ошибка подготовки/окружения/консоли/artifacts либо изменение исходников/HEAD |
| TIMEOUT | Host лимит исполнения превышен, группа процессов остановлена |
| INTERRUPTED | После сбоя исход исполнения неопределён; требуется явный retry |
| REJECTED | Профиль отсутствует или revision не совпадает |
| CANCELLED | Worker учёл durable отмену и остановил собственное исполнение |

Агент принимает terminal result только после проверок identity/hash/size всех
artifacts, final status и полного streaming log. Клиентский `verified` не является
полем remote result. Upload pending — локальное состояние доставки, не CI FAIL.

## Отмена и поздние результаты

`ci/jobs/<job-id>/cancel.json` — immutable canonical JSON:
`{cancel_version:1, repository_id, job_id, worker_id, request_sha256}`.
Он привязан к точным bytes исходного request. Клиент проверяет поддержку
`cancel_version:1` исходным worker, готовит файл через gdi, загружает и проверяет
read-back. Доставка marker означает CANCEL_REQUESTED, не подтверждённую остановку.
Проверенный конечный результат, полученный до подготовки отмены, возвращается
без создания marker. Параллельные отмена/завершение разрешаются консервативно:
появившаяся поддерживаемая отмена блокирует обычный результат этой попытки.

Worker сохраняет marker локально до остановки. Проверки выполняются до старта,
во время CI, при возобновлении доставки, между загрузками artifacts и до/после
загрузки result. Сбой проверки не считается отсутствием отмены. Клиент тоже
повторяет проверку после скачивания artifacts; уже проверенный immutable marker
сохраняется локально и не отменяется последующим исчезновением файла на Drive.

Если result ещё не подготовлен, CANCELLED публикуется обычным порядком. Если он
уже записан, исходные result/log/artifacts не перезаписываются: собственные
процессы/контейнеры останавливаются, а подтверждение отмены публикуется в
`ci/jobs/<job-id>/cancelled/`. Эта папка содержит обычную схему result со state
CANCELLED, собственные build.log/final-status.json и log-chunks. Descriptor paths
считаются относительно этой папки, identity/request_sha256/run_id сохраняются.
Result здесь также загружается последним и проверяется полным общим валидатором;
существующий claim должен соответствовать run_id. Клиент выбирает этот результат
только вместе с проверенной отменой. Без подтверждения возвращается
CANCEL_REQUESTED, verified:false; обычный поздний PASS не применяется через
pull --passed. Все clients, проверяющие отменённые задания, должны поддерживать
эту проверку; старые clients могут игнорировать новый cancel marker.

`ci cancel --withdraw` позволяет убрать точное уведомление старого worker без
поддержки отмены. В marker добавляется только в этом случае
`worker_cancellation_supported:false`; такой marker не подтверждает остановку,
не блокирует проверенный обычный результат и не разрешает retry активного CI.
Request/ready/claim/история не удаляются. Для поддерживаемого worker retry
разрешается по проверенной отмене либо terminal result и сохраняет связь retry_of.
Недоступность host не доказывает остановку offline CI; повторяемость задания
учитывается отдельно. Автоматические таймауты/переназначение и retention — TODO.

## Recovery и GC

Ledger PUBLISHED фиксируется после read-back проверки result/artifacts, затем
удаляется конкретный marker: v2 — notification общей inbox, legacy v1 —
`ci/queue/<job-id>.json`. Crash между этими действиями исправляется
при discovery, без исполнения. Request/ready/claim/chunks/result остаются в архиве.

При RUNNING/FINALIZING без durable result нет автоматического rerun:
INTERRUPTED либо CANCELLED при проверенной отмене.
Сохранившийся result допубликовывается независимо от прежнего ledger state,
после проверки отмены; при отмене доставляется отдельный CANCELLED. Повреждение
локального закрытого artifact/chunk блокирует доставку и не вызывает повтор CI.

Apply-GC bundles отказывает при legacy queue pointer, неполном/nonterminal job v1/v2
или неподтверждённой поддерживаемой отмене. Поздний обычный PASS не снимает защиту;
подтверждение cancelled/result.json учитывается с его marker и artifacts. Completed
CI artifacts не удаляются. Все clients и worker должны быть остановлены; snapshot
checks не заменяют распределённый lock. Retention CI namespace пока не реализован.

`--bind=false` означает копию исходников для act jobs. Post-check host checkout
не доказывает неизменность container workspace во время CI: workflows могут
генерировать/менять файлы внутри своей копии и явно получать другие repositories.
PASS относится к исполнению выбранного workflow из закреплённого commit, а не
к запрету таких действий. Версии external actions/images определяет проектный YAML.
