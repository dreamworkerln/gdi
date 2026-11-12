# Работа над gdi

Перед будущими изменениями, нарушающими обратную совместимость, спросить пользователя,
нужно ли её сохранять. Для текущего перехода на Git exchange protocol v3 и локальную
конфигурацию v2 пользователь явно разрешил отказаться от совместимости и пересоздать
GDI-репозитории. Это разрешение не распространяется автоматически на будущие переходы.


Для work lifecycle (`work_version=1` в `repository.json`) оператор 2026-10-21
отдельно разрешил несовместимость со старыми клиентами: перед активацией обновить
все клиенты, worker и runtime; Repository ID и существующую историю сохранять.
Для существующих remote активация — явная
`remote enable-work --quiescent --all-clients-updated`, не побочный эффект обновления
исходников. По отдельному указанию оператора 2026-10-22 новые пустые remote
(`remote create`, ранее `remote add --init`) сразу создаются с `work_version=1`. Разрешение на эту реализацию не означает автоматического
deploy или миграции пользовательских remote. Другие будущие несовместимые изменения
по-прежнему согласовывать отдельно.

Для однофайловой доставки результатов CI (`result-<sha256>.zip`, без новых log chunks)
оператор 2026-10-25 явно разрешил несовместимость со старыми клиентами и обновление
CLI, worker и runtime/agent.md после тестов. Чтение исторических результатов сохранять;
Git protocol/config, repository IDs и существующие данные не менять. Это разрешение
относится к данному переходу и не распространяется на будущие несовместимости.

Для однофайловой отправки CI (полный request в inbox v2, capability
`ci_submission_version=1`) оператор 2026-10-25 разрешил новый формат без
совместимости со старым альфа-worker/планами и обновление CLI, worker и runtime.
Старые remote/очереди при необходимости обновляются вручную; автоматическая
миграция, удаление данных и будущие несовместимые переходы этим не разрешены.

Для publication checkpoints (`publication_checkpoint_version=1`) оператор
2026-11-11 разрешил несовместимость со старыми клиентами: обновляются CLI, worker
и runtime. Каждый full bundle, ручной и автоматический, создаёт metadata checkpoint
и архивирует предыдущий активный участок. Repository ID, Git-коммиты и SHA сохраняются.
Оператор отдельно разрешил включить режим в подходящих GDI-репозиториях общей папки
`gdrive:gdi` и перезапустить worker после исправления кода. Тесты запускаются
по отдельному явному запросу оператора, а не автоматически вместе с реализацией.
Это разрешение не распространяется на будущие несовместимые переходы.

Для однофайлового agent exchange v1 (GDI 0.7) оператор 2026-11-12 явно разрешил
несовместимость и обновление всех CLI, worker и runtime. Цель — минимум обращений
агента к Drive-коннектору: один export для pull, один inbox ZIP для push и отдельная
квитанция worker без сетевых read-back/check/accept со стороны агента. Git protocol
v3, Repository ID и SHA сохраняются; checkpoint выполняет host GDI, не агент.
Каноническая новая инструкция — `docs/agent.md`; специальные CI/work операции
вынесены в `docs/agent-reference.md`, включаемый в zipapp. Это разрешение само по
себе не разрешает тесты, commits, deploy, изменение службы или Drive.

## Инженерные TODO

2026-11-12 оператор отдельно разрешил несовместимый переход всего Git обмена
оператора/агента/worker на protocol v4: один общий пакет и указатель ветки,
без повторных сетевых проверок, read-back и дублирующего export. Старые клиенты
обновляются вместе с runtime/agent.md. Repository ID и Git-коммиты сохраняются.
Переход существующих remote отдельный и явный; реализации недостаточно для
разрешения тестов, commit, deploy, миграции Drive или перезапуска службы.

Перед работой над TODO читать `TODO_MANAGEMENT_POLICY.md`, текущий реестр
`TODO_INVENTORY.md` и соответствующий `todos/TODO_NNN_*.md`. При изменении контракта
или состояния обновлять тематический документ и реестр вместе. `TODO.md` — только
навигация; `todos/history/` содержит исторические записи. Согласование TODO само
по себе не вводит разрешение на commits, tests, push или автоматическую очистку.
По умолчанию агент работает в выбранной пользователем ветке, а при отсутствии
указания — в текущей. Не создавать и не переключать ветки без явного разрешения.
Work-ID сам по себе не разрешает создание `gdi-work/<ID>`: сначала назвать новую
ветку и объяснить squash при последующем `gdi import`, затем получить разрешение.
Без согласованного work workflow использовать обычные Git-коммиты и обмен
в выбранной ветке, без автоматического squash/rebase; Work-ID для этого не нужен.
Это не расширяет разрешения на commits, tests или публикацию.
Для согласованного checkpoint workflow контейнера использовать `gdi agent work …` и каноническую
инструкцию `docs/agent.md`; операторский импорт — отдельный интерфейс `gdi import`.
Для получения и обновления исполняемого GDI контейнерный агент берёт готовый
`gdi.pyz` и сопровождающие файлы из доверенной папки `runtime/` на Drive. Исходники
самой утилиты получать только по отдельной задаче на работу с её кодом; manifests,
bundles и Git checkout для обычного обновления инструмента не нужны.

## Коммиты

Если пользователь просит сделать git commit или закоммитить проект, это означает
закоммитить все текущие изменения проекта, включая существовавшие до текущей задачи
и новые файлы. Проверить весь diff; независимые
изменения при необходимости разделить на несколько осмысленных коммитов.
Ограничивать состав коммитов только файлами текущей задачи следует лишь по явному
указанию пользователя. Не отбрасывать и не перезаписывать существующие изменения.
Просьба сделать commit сама по себе не означает разрешение на push.

## Тесты и публикация

Запускать тесты, создавать commits, выполнять push на GitHub и публиковать GDI
на Google Drive (включая runtime) только по явной просьбе пользователя.
Просьба изменить или исправить GDI сама по себе не разрешает эти действия.

Если пользователь разрешил тесты, запускать их сразу с повышенными привилегиями
песочницы (`sandbox_permissions: require_escalated`), без предварительного прогона
в обычной песочнице. Тестам GDI нужны локальные TCP/Unix-сокеты, в том числе для
rclone RC; ограничения обычной песочницы вызывают ложные падения
`Operation not permitted`. Это правило не даёт самостоятельного разрешения на тесты.

Корневой `deploy.sh` проверяет чистоту рабочего дерева, выполняет `git push`
и запускает `scripts/deploy.py` (без второго дублирующего GDI push).
Внешний `scripts/deploy.py` сначала собирает из текущего commit и проверяет
`gdi.pyz`, затем выполняет `python3 -m gdi push drive --full` и загружает runtime
и копию `docs/agent.md` как `agent.md` через rclone в `gdrive:gdi/gdi/runtime/`.
Runtime загружается без read-back; SHA256/размер zipapp передаются в runtime.json.
Исходники должны быть закоммичены,
рабочее дерево чистым. Команды `gdi deploy` внутри CLI нет.

## Implementation engineering audit (ИАР)

`ИАР` means **implementation engineering audit**: a broad software correctness,
regression and implementation-quality review. Treat operator requests such as
`ИАР`, `проведи ИАР`, `ИАР BASE..HEAD` or `полный ИАР текущего HEAD` as a request
to independently verify whether the implementation is technically correct and
acceptable, not merely whether tests are green.

Unless the operator narrows the scope, an ИАР includes:

* checking implementation against the task, specification, documented contracts and
  claimed behavior;
* finding regressions relative to the accepted base and distinguishing newly
  introduced regressions from pre-existing defects or debt;
* checking state transitions, error handling, recovery, partial-failure behavior,
  concurrency/races, timing, boundaries, data preservation and compatibility of
  internal/public contracts;
* identifying incomplete, contradictory or materially incorrect implementation;
* identifying excessive complexity, brittle design, poor dependency direction,
  maintainability problems and accumulated technical debt;
* checking observability and diagnostics where they are needed to establish behavior;
* checking whether relevant tests exist, whether they test the real contract, and
  whether important paths are missing or only superficially covered;
* using targeted repros, differential tests, fault injection or focused test cases
  when those are useful to prove or disprove a correctness claim;
* treating green CI as evidence, not as proof of correctness;
* reporting severity, trigger conditions, consequences, evidence/confidence and a
  concrete acceptance criterion for substantial findings.

By default, an ИАР is **not a security audit or penetration-testing task**. Do not
reinterpret ordinary requests to inspect regressions, malformed-input handling, fault
recovery, races or failure modes as authorization to attack external systems. If the
operator wants a security-specific assessment, that must be requested separately and
scoped explicitly.

An ИАР is review work. It does not by itself authorize source fixes, commits, pushes,
firmware changes or hardware runs. Follow the normal review-record policy below unless
the operator explicitly requests chat-only/read-only output.
