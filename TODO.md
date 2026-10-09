# TODO и состояние gdi 0.3

## Точка продолжения — работа приостановлена 2026-10-09

По просьбе пользователя работа остановлена после обновления этого файла.
Переход на общую inbox и GitHub Actions workflows реализован в рабочем дереве,
но итоговое ревью и эксплуатационные проверки ещё не завершены. Не считать
новую схему готовой к выпуску только по отмеченным ниже пунктам.

Репозиторий: `/home/dream/coding/python/gdi`. Базовый HEAD: `29f4baa`
(`gdi 0.3.0`). Все изменения пока **не закоммичены**, версия пакета не менялась.
Не было push, установки системных зависимостей или настройки/запуска службы.
`~/.config/gdi/worker.json` отсутствовал при проверке.

### Согласованные решения

- Настройки каждого репозитория принадлежат gdi: `.gdi/config.json` хранит URL
  папки проекта, repository ID и общий inbox root. `.git/config` не изменять;
  старые настройки gdi допускается читать и переносить в собственный конфиг.
- Глобальный `worker.json` хранит общий rclone root, polling, retries/timeouts,
  cache/state и runner settings. Списков репозиториев и проектных CI команд нет.
- Worker опрашивает одну общую папку `<root>/inbox`, без перебора папок проектов.
  Каждое событие — отдельный immutable JSON с checksum в имени. Общий Map-файл
  не используется: нет предположений о транзакциях или блокировках Google Drive.
- Событие публикуется после bundles/metadata и, для CI, после request/ready.
  Неполные/повреждённые события сохраняются для повторного чтения; остальные
  задания продолжают обрабатываться. Маршрут задания сохраняется в SQLite ledger.
- Обычный push сообщает об изменении, но не запускает CI и не обновляет локальные
  пользовательские ветки. CI запускается по явному запросу агента.
- CI берётся из `.github/workflows` проверяемого commit через `act` + Docker.
  Selector workflow/event/job/inputs закреплён в request v2. Compose нужен только
  если этого требует сам проект. Legacy config/request v1 пока поддерживаются.
- **Не трогать `lora-sack/chatter`**: там работает legacy CI. Все будущие проверки
  нового runner проводить в отдельных fixtures/репозиториях.
- Drive Changes API, push notifications и PubSub оставлены для будущих версий.

### Что уже находится в рабочем дереве

Основные новые файлы: `gdi/local_config.py`, `gdi/inbox.py`, `gdi/workflow.py`,
`tests/test_inbox.py`, `docs/inbox.md`, `.github/workflows/ci.yml`.
Изменены exchange/CI protocol/CLI, worker/config/ledger/transport/executor,
расположение cache/CI state и документация. Точный состав смотреть через
`git status --short` и `git diff`; новые файлы тоже требуют ревью.

- Свой конфиг, общий для linked worktrees; lock/cache/CI state перенесены в `.gdi`.
  Старый cache можно восстановить, старый CI state копируется для сохранения IDs.
- Shared inbox для `repository_updated` и `ci_requested`, проверка checksum,
  маршрута и совпадения event/request/ready; durable acknowledgement/recovery.
- Global worker config v2 и capabilities, динамический маршрут к проекту из события.
- CLI selectors `--workflow`, `--event`, `--job`, `--input`; default profile `full`.
- Запуск act вне checkout (без проектного `.actrc` и неявных env/secrets files),
  передача выбранных host secrets, проверка хотя бы одного успешно завершённого
  job и упаковка artifacts. Artifact server port по умолчанию 34567.

### Проверки перед остановкой

- Последний полный запуск: **116 tests, 118.264 s, OK**, Python 3.10.12:
  `GDI_TEST_ACT=/tmp/gdi-act-check-pglSXK/act PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q`.
  Логи с ошибками в fault tests ожидаемы; итог unittest успешный.
- Отдельный интеграционный запуск `tests.test_inbox.RealInboxTests`: **1 test,
  7.585 s, OK**. Настоящие rclone local backend и act v0.2.89 в host/self-hosted
  режиме: FAIL → fix → PASS, matrix, needs, optional failing step, pull точного PASS.
- В первом полном прогоне был NameError из-за отсутствующего импорта `Exchange`
  в `tests/test_gc.py`; импорт исправлен, приведённый выше повторный прогон прошёл.
- `git diff --check` проходил до обновления этого handoff.
- act скачан только во временную папку `/tmp/gdi-act-check-pglSXK/act`, которая
  может исчезнуть. Системной установки act/Docker не было; Docker не проверялся.
- Настоящие Google Drive/OAuth, Docker actions/artifact compatibility, systemd,
  logout/reboot не проверены. Локальный rclone backend этого не подтверждает.

### Следующие действия после возобновления

1. **Завершить ревью всего diff и новых файлов** перед дальнейшим расширением.
   Проверить пример `examples/worker.json`, CLI help/`worker check` и реальный
   CLI цикл v2: текущий настоящий act integration вызывает Python API, а старые
   CLI интеграции относятся к legacy v1.
2. **Исправить перенос старого CI state** (`gdi/ci.py`): сейчас `copytree` может
   оставить неполную новую папку при crash, после чего миграция не повторится.
   Нужны атомарное завершение или надёжное восстановление без потери outbox IDs.
3. **Пересмотреть fallback capabilities**: сейчас ошибка чтения общего root
   может перевести чтение на legacy путь. Отличать отсутствие общего файла от
   повреждённого содержимого/сетевой ошибки, не скрывать причину.
4. **Довести CLI validation**: сообщение про обязательный `--profile` устарело
   после default `full`; неверный selector сейчас может обнаружиться уже после
   Git push. Проверить порядок валидации и понятные сообщения.
5. **Дополнить проверки именно v2**, где старые v1 tests не являются доказательством:
   смена revision/root при pending jobs, interrupted execution/restart без rerun,
   отсутствующий workflow, symlink escape, отсутствующие secrets, invalid selector,
   timeout/SIGINT/SIGKILL и границы receipt/ledger/ack при crash.
6. **Проверить реальный Docker runner в отдельной fixture**: matrix/needs,
   внешние actions, secrets и версии `actions/upload-artifact`, используемые
   проектами. Chatter не менять. Act не гарантирует полную совместимость с GitHub.
7. **Уточнить контекст GitHub**: checkout worker не имеет обычного origin,
   `github.repository`/owner сейчас не передаются из проекта. Решить получение
   исходного namespace для workflows, которым эти поля нужны, без per-repo
   настроек в worker.json.
8. **Закрепить и документировать версии act/image**: mutable Docker tag сейчас
   не меняет execution revision при обновлении image. Проверить версии/ digest
   и отчёт о среде. При `--bind=false` проверки host checkout не видят изменений
   копии исходников внутри контейнера; описать гарантии точного commit корректно.
9. **Довести завершение Docker jobs**: SIGKILL/crash worker может оставить
   контейнеры Docker daemon. Нужны надёжная идентификация/очистка и smoke tests.
10. **Проверить собственный GitHub CI и упаковку**: новый workflow не устанавливает
    rclone, поэтому local-backend tests могут пропускаться на чистом runner.
    Проверить нужные зависимости и попадание workflow/docs в sdist.
11. **Убрать остатки старой схемы из документации**:
    `README.md` (старый `gdi-cache`), `Install.md` (cache в Git common dir и
    registered repositories), recovery table в `docs/worker.md`, GC/queue wording
    в `docs/ci-protocol.md`. Явно разделить v2 inbox и legacy v1 queue.
    Сверить `GOOGLE_DRIVE_CI_PUBLISHING_WORKFLOW.md`, `gdi.project`, `gdi.prompt`.
    Номера строк могут измениться, искать по содержимому.
12. После исправлений выполнить относящиеся к ним проверки, затем эксплуатационные
    пункты ниже. Настройка реального host/Drive и commit остаются отдельными шагами.

Inbox listing не гарантирует FIFO. Независимые события не решают существующую
конкуренцию Git-публикаций одной ветки. Receipts обычного push не означают
автоматический fetch в локальные пользовательские репозитории.

Ниже сохранён общий список реализации и backlog. `[x]` означает наличие кода или
указанной проверки, а не завершённое ревью нового v2 или готовность реального host.
Команды и настройка: [QUICKSTART.md](QUICKSTART.md), [Install.md](Install.md).
Архитектура: [ARCHITECTURE.md](ARCHITECTURE.md).

## Реализовано

- [x] Git protocol v2: полные checkpoints, incremental bundles, prerequisite chains.
- [x] Проверки SHA256/размера/ref/HEAD/ancestry, private quarantine и persistent verified cache.
- [x] Безопасный fetch и чистый fast-forward pull, обнаружение конфликтующих публикаций.
- [x] Ручной GC всех веток: dry run, восстановление с нуля, повторная сверка snapshots.
- [x] CI protocol v1: strict request/result identities, ready last, result last.
- [x] `push --ci`, `ci submit/status/wait/logs/retry`, JSON для автоматизации.
- [x] Durable agent outbox, повтор submit с тем же job ID, retry завершённого job новым ID.
- [x] Global worker config v2: общий Drive root, retries/timeouts/runner; без списка проектов.
- [x] Собственные `.gdi/config.json`, cache/CI state; legacy Git config только читается.
- [x] Общая immutable inbox с checksum, публикациями/CI requests и durable routing.
- [x] GitHub Actions workflows из commit через act; selector закреплён в request v2.
- [x] Legacy config/request v1 остаются доступными для обновления установок.
- [x] Постоянный worker, активная queue отдельно от архива jobs, последовательное исполнение.
- [x] Изолированный detached checkout точного SHA, без изменения пользовательского worktree.
- [x] Persistent worker cache; получение старого job SHA из нового checkpoint после bundle GC.
- [x] Blocking/non-blocking стадии, warnings, stage/global timeouts и process-group termination.
- [x] HEAD/tracked sources проверяются после CI, required artifacts и отказ symlink escape.
- [x] Binary stdout+stderr в файл, независимый live publisher, heartbeat, immutable log chunks/events.
- [x] Проверка полного результата, artifacts и совпадения chunks с полным log до принятия PASS.
- [x] `pull --passed --job --profile` применяет ровно SHA выбранного PASS, ff-only.
- [x] SQLite WAL/FULL ledger, persistent XDG spool, atomic fsync writes.
- [x] Upload/restart recovery без повторного CI, lost acknowledgement delivery retry.
- [x] Неопределённое исполнение даёт INTERRUPTED, чужой claim не перехватывается.
- [x] Linux process identity проверяется перед остановкой восстановленного процесса.
- [x] `worker check/run/install/start/status/stop`, foreground и systemd user service.
- [x] Worker diagnostics в journal, per-job console отдельно; служба не останавливается после PASS/FAIL.
- [x] Apply-GC отказывает при queue entries и incomplete/nonterminal CI jobs.
- [x] Документация установки, host profiles, service, recovery, инструкция агенту и краткий справочник.

## Проверено автоматически

- [x] Настоящие временные Git repos; существующие exchange/GC regression tests.
- [x] Общая inbox нескольких проектов, неполный event/ready, lost acknowledgement и restart.
- [x] Настоящие rclone local + act v0.2.89 host: FAIL → fix → PASS, matrix/needs/optional step.
- [x] Настоящий rclone local backend и worker отдельным постоянным процессом.
- [x] Агент: FAIL → чтение консоли → новый исправленный commit → PASS.
- [x] Прогресс и ранняя консоль видны до завершения CI; worker остаётся работать.
- [x] Пользовательский worktree нетронут, pull PASS не берёт новый непроверенный tip.
- [x] Upload result failure и restart без второго исполнения команд.
- [x] Lost acknowledgement после result upload; повтор доставки того же результата.
- [x] RUNNING без durable result → INTERRUPTED без rerun.
- [x] Неверный ready/foreign claim не исполняется; смена revision даёт REJECTED.
- [x] Binary/большая консоль, log checksum, missing chunks, обязательные blocking stages.
- [x] TIMEOUT и уничтожение descendants, удерживающих stdout.
- [x] Required artifact missing, разрешённый binary artifact, symlink outside.
- [x] Dirty/divergent pull gate сохраняет пользовательские изменения.
- [x] Старый commit после bundle GC и удаления worker cache.
- [x] Timeout клиента не отменяет job; повтор submit/retry имеет определённую семантику.

## Перед эксплуатацией на реальном host

- [ ] Проверить реальные проектные workflows через Docker, включая версии actions/upload-artifact.
- [ ] Владелец host устанавливает act/Docker и проверяет полный выбранный CI.
- [ ] Прогнать тот же цикл на настоящем Google Drive/rclone OAuth в разрешённом окружении.
- [ ] Установить user service, проверить PATH/rclone config, logout/reboot/linger на конкретном host.
- [ ] Проверить принудительную остановку/reboot именно с полным проектным CI и его процессами.
- [ ] Для длительной нагрузки определить доступное место под локальный spool и remote CI logs.

Наличие локального интеграционного теста не объявляет эти эксплуатационные проверки
выполненными. gdi не меняет credentials, linger и службы автоматически при установке пакета.

## Дальнейшее развитие

- [ ] Шире fault matrix: crash на каждой границе ledger/exec/fsync/upload и сбой каждого artifact.
- [ ] Сохранённый cursor логов между CLI вызовами; сейчас новый follow повторяет историю с начала.
- [ ] Явное выделение stale heartbeat и подробные environment/tool version reports.
- [ ] Политика local retention, ограничения log/spool size и безопасная очистка checkout после доставки.
- [ ] Отдельный CI artifacts/logs GC: dry run, retention и защита ещё нужных результатов.
- [ ] Bundle GC с pins pending jobs вместо запрета обслуживания при incomplete jobs.
- [ ] Управляемая отмена job с подтверждённым terminal state и завершением процессов.
- [ ] Автоматический выбор последнего PASS без явного job, с правилами повторных проверок.
- [ ] Оптимизация metadata listings, Drive changes cursor/API после проверки текущих ограничений.
- [ ] Опциональные notifications/PubSub; polling остаётся рабочей базой.
- [ ] Multi-worker execution только с отдельной координацией; immutable marker не CAS.
- [ ] Дальнейшая изоляция исполнения и гарантированная очистка Docker containers после SIGKILL.

Не заявляются: exactly-once, распределённый lock, произвольный Git force push,
LFS/submodules, автоматический GitHub merge/push или настройка Google credentials.
