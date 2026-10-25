# CI protocol v1/v2 — gdi 0.4

Отдельный от [Git protocol v3](protocol.md) namespace `ci/`. Один назначенный
worker на запрос; несколько workers общего root используют разные worker_id.
Legacy v1 — на repository remote. Все job IDs/run IDs — 32 lowercase hex, Git SHA — 40,
publication IDs/profile revisions/SHA256 — 64. Worker/profile IDs: ASCII letters,
digits, `_`, `-`, до 64 символов. JSON UTF-8, canonical encoding как у Git metadata;
duplicate keys и metadata больше 1 MiB отвергаются. Неизвестные request/result fields
отвергаются. Доступ участников к Drive доверенный, подписи/авторство не проверяются.

## Самодостаточная inbox и request v2

Глобальный worker объявляет в общем root capabilities с ci_version:1,
inbox_version:1, ci_submission_version:1, cancel_version:1, result_delivery_version:1,
worker_id и profiles:{full:revision}. Inbox v1 остаётся форматом уведомления Git;
для CI используется оболочка inbox_version:2 с полным request внутри.

Request содержит ci_version:2, job_id, repository_id, ref, head, publication_id,
worker_id, profile_id, profile_revision, created_at, retry_of и workflow.
Workflow — точные {path:".github/workflows", event:"push", job:"", inputs:{}}.
Необязательное github_repository содержит только owner/repository. В request нет
executable/argv, host paths, credentials или secrets; selector выбирает YAML
из checkout точного commit. Namespace сохраняется при retry и входит в hash.
Новый повтор — новый job ID с retry_of; повтор доставки сохраняет прежний ID.

Оболочка содержит repository_path относительно общего root и дублируемые identity
поля; они обязаны совпасть с request, request_sha256 — с его canonical bytes.
Имя inbox-файла: <job-id>-<sha256-всей-оболочки>.json. Неверный hash/schema,
дубликаты job ID или расхождения identity отклоняются до исполнения.

Порядок: durable outbox → один upload inbox → read-back proof. Нет отдельных
request/ready uploads. Worker проверяет свежую repository identity, фиксирует
request/route/notification в ledger, затем архивирует один submission.json
в папке job и проверяет его bytes. Исполнение начинается после этого архива.
При сбое передачи и restart сохраняются job ID, run ID, request и route.

Offline agent: prepare → upload files.inbox → accept по полному inbox proof.
Для accept используются source snapshot и capabilities из prepare; дополнительный
snapshot папки job и промежуточный ci check не требуются. Свой запрос reader берёт
из сохранённого плана/локального индекса, для неизвестного job — из submission.json
либо (обычный online CLI до регистрации worker) из общей inbox.

Claim worker.running.json immutable: ci_version:1, job_id, run_id, worker_id,
request_sha256. Ledger сохраняется до claim upload; чужой run не перехватывается.
Отмена/successor по-прежнему привязаны к точному request_sha256.

2026-10-25 оператор разрешил новый альфа-формат без совместимости со старым
worker/многофайловыми планами и поручил обновить CLI, worker и runtime.
Старые repo/очереди при необходимости обновляются вручную, автоматической миграции
или удаления нет. Git protocol/config и repository ID сохраняются.

## Status и heartbeat

Job `status.json` и worker `status.json` mutable advisory. Поля job status:
`ci_version`, `job_id`, `run_id`, `worker_id`, `request_sha256`, `state`, `stage`
(имя либо null), `sequence` (возрастающий integer), `updated_at` (UTC ISO timestamp).
Во время исполнения worker публикует только job/worker status с heartbeat.
Отдельные events и log-chunks для новых результатов не создаются. Sequence
увеличивается и при неизменном этапе; status остаётся advisory.

Расширенный worker status имеет `registry_version: 1`, `state: READY|BUSY`, `busy`,
`queue_length`, `profile_revisions`, `labels`, `platforms` (shared inbox).
Busy snapshot сохраняет job/run/request identity и timestamp захваченного job heartbeat;
idle snapshot имеет свежий timestamp без job identity. Политика доступности и переноса:
[scheduling.md](scheduling.md). Старые worker snapshots годятся для диагностики,
но не подтверждают доступность для автоматического выбора.

Клиент вычисляет диагностический `heartbeat` (состояние свежести, возраст, порог)
для незавершённого status/result; registry inspection добавляет `reason_code` и
детали несовместимости. Это поля ответов tools, они не записываются в wire
status/request/result. Устаревший heartbeat не является terminal state или
доказательством остановки исполнения.

Автоматическая замена использует дополнительный immutable
`ci/jobs/OLD_JOB_ID/successor.json`: `successor_version:1`, исходные `job_id` и
`request_sha256`, `policy_sha256` и полный точный новый `request`. Worker и клиент
сверяют predecessor identity, durable cancellation и точный successor request.
Этот файл фиксирует proposal до новой inbox, сохраняя прежнюю схему request v1/v2;
он не является CAS. Сохраняйте исходные requests/cancellations и историю попыток.

Консоль — объединённые stdout+stderr, bytes без преобразований. Для act это JSON
lines; в результате одна blocking-стадия github-actions, детализация jobs/steps
содержится в полном build.log. Во время исполнения лог хранится только на worker;
локальная команда worker job logs сохраняет live-доступ без обращений к Drive.
Удалённый wait --follow показывает stage/heartbeat, затем полный проверенный лог.

Worker закрывает console file и fsync до фиксации результата. Mutable status с PASS
сам по себе никогда не является доказательством успешной проверки.

## Result и однофайловая доставка

Новый terminal result публикуется одним immutable файлом
`result-<sha256>.zip` в ci/jobs/<job-id>/ либо в cancelled/ для поздней отмены.
SHA256 вычисляется по полным bytes архива и входит в его имя. Отдельный checksum
или descriptor на Drive не нужен. Полный listing папки должен содержать ровно один
пакет; несколько пакетов или некорректное имя result-* приводят к отказу.

Внутри ZIP находятся package.json с package_version:1, canonical result.json
и точные файлы из artifacts descriptors, включая build.log и final-status.json.
Других записей, директорий, повторяющихся имён, symlinks или encrypted entries
быть не должно. ZIP допускает stored/deflated entries; упаковка детерминирована.
GDI проверяет SHA256 всего пакета, identity/PASS gates и inventory/размеры/hashes
каждого внутреннего файла; extraction потоковая, без extractall и загрузки всего
лога/больших artifacts в память. Новые пакеты не содержат chunks.

Порядок: durable log/artifacts/final status → локальный result.json → сборка пакета
→ upload пакета → read-back/проверка → ledger PUBLISHED → удаление inbox/queue.
Отдельные result.json, artifacts, final-status.json и build.log на Drive для новых
jobs не публикуются. Ошибка упаковки/upload/read-back не вызывает повторный CI.
Capabilities нового worker содержат result_delivery_version:1.

2026-10-25 оператор разрешил несовместимость нового обмена со старыми клиентами:
обновить CLI, worker и runtime/agent.md до использования. Git/config protocol и
repository IDs не меняются; удаление истории/миграция remote не требуются.
Новый reader сохраняет чтение исторических result.json с отдельными файлами
и прежней проверкой полной последовательности chunks. История не переписывается.

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
artifacts, final status, полного binary log и пакета доставки. Клиентский `verified` не является
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
во время CI, при возобновлении доставки, до/после упаковки, загрузки и read-back пакета. Сбой проверки не считается отсутствием отмены. Клиент тоже
повторяет проверку после скачивания и проверки пакета; уже проверенный immutable marker
сохраняется локально и не отменяется последующим исчезновением файла на Drive.

Если result ещё не подготовлен, CANCELLED публикуется обычным порядком. Если он
уже записан, исходные result/log/artifacts не перезаписываются: собственные
процессы/контейнеры останавливаются, а подтверждение отмены публикуется в
`ci/jobs/<job-id>/cancelled/`. Эта папка содержит собственный result-<sha256>.zip
со state CANCELLED, полным build.log/final-status.json и artifacts внутри.
Descriptor paths считаются относительно корня пакета; identity/request_sha256/run_id
сохраняются. Пакет проверяется полным общим валидатором;
существующий claim должен соответствовать run_id. Клиент выбирает этот результат
только вместе с проверенной отменой. Без подтверждения возвращается
CANCEL_REQUESTED, verified:false; обычный поздний PASS не применяется через
pull --passed. Все clients, проверяющие отменённые задания, должны поддерживать
эту проверку; старые clients могут игнорировать новый cancel marker.

`ci cancel --withdraw` позволяет убрать точное уведомление старого worker без
поддержки отмены. В marker добавляется только в этом случае
`worker_cancellation_supported:false`; такой marker не подтверждает остановку,
не блокирует проверенный обычный результат и не разрешает retry активного CI.
Submission/claim/история не удаляются. Для поддерживаемого worker retry
разрешается по проверенной отмене либо terminal result и сохраняет связь retry_of.
Недоступность host не доказывает остановку offline CI; повторяемость задания
учитывается отдельно. Автоматические таймауты/переназначение и retention — TODO.

## Recovery и GC

Ledger PUBLISHED фиксируется после read-back проверки итогового пакета, затем
удаляется конкретный marker: v2 — notification общей inbox, legacy v1 —
`ci/queue/<job-id>.json`. Crash между этими действиями исправляется
при discovery, без исполнения. Submission/claim и пакет результата остаются на Drive.

При RUNNING/FINALIZING без durable result нет автоматического rerun:
INTERRUPTED либо CANCELLED при проверенной отмене.
Сохранившийся result допубликовывается независимо от прежнего ledger state,
после проверки отмены; при отмене доставляется отдельный CANCELLED. Повреждение
локального закрытого artifact/лога/пакета блокирует доставку и не вызывает повтор CI.

Apply-GC bundles отказывает при legacy queue pointer, неполном/nonterminal job v1/v2
или неподтверждённой поддерживаемой отмене. Поздний обычный PASS не снимает защиту;
подтверждение в cancelled/ учитывается с его marker и проверенным пакетом. Completed
CI artifacts не удаляются. Все clients и worker должны быть остановлены; snapshot
checks не заменяют распределённый lock. Retention CI namespace пока не реализован.

`--bind=false` означает копию исходников для act jobs. Post-check host checkout
не доказывает неизменность container workspace во время CI: workflows могут
генерировать/менять файлы внутри своей копии и явно получать другие repositories.
PASS относится к исполнению выбранного workflow из закреплённого commit, а не
к запрету таких действий. Версии external actions/images определяет проектный YAML.
