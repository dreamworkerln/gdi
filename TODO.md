# TODO и состояние gdi 0.3

## Завершение текущих 12 шагов — 2026-10-09

По запросу пользователя возобновлены текущие 12 шагов. Раздел «Дальнейшее развитие»
остаётся backlog. Репозиторий: `/home/hatuncevk/coding/python/gdi`, ветка `dev`,
исходный HEAD `4cbd6ac` (`refactored`). Прежний handoff про незакоммиченную схему на
`29f4baa` устарел: схема v2 уже находится в `4cbd6ac`.
Новые исправления пока в рабочем дереве; commit и публикация проекта не выполнялись.

### Продолжение эксплуатационных проверок — 2026-10-09

По новому запросу пользователя продолжены оставшиеся пункты; разрешены отдельный
Drive root `rclone:gdi-acceptance`, установка/start user service и linger.

- Docker daemon теперь использует HTTP/HTTPS proxy. Реальные `docker run --rm
  hello-world` и `docker pull catthehacker/ubuntu:act-latest` прошли успешно.
  DNS host по-прежнему без серверов, но загрузка через daemon proxy работает.
- Docker acceptance: **2 tests, 171.494 s, OK** — matrix/needs/secrets,
  checkout, upload-artifact v3/v4, FAIL → fix → PASS/exact pull и SIGKILL/restart.
  Первый прогон упёрся в 60-секундный лимит загрузки external actions; Docker
  fixtures теперь используют 600 секунд для CI и 720 для CLI wait.
- act v0.2.89 установлен в `/home/hatuncevk/.local/bin/act`; SHA256
  `6be37b104430efc210d5130495bedcff2dc7cd6780a38d88f3d205e7f1185cc1`.
- Образ закреплён в постоянном worker config как
  `catthehacker/ubuntu@sha256:c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43`.
  `worker check --runtime` прошёл на Docker 29.9.0.
- Созданы `~/.config/gdi/worker.json` и приватный `worker.env`, установлена
  `gdi-worker.service`: **active/running/enabled**. Worker `hatuncevk-acceptance`
  обслуживает только `rclone:gdi-acceptance/service`. User manager сохраняет
  старые группы, поэтому unit override использует `sg docker`.
- Linger включён по прямому ответу пользователя: **Linger=yes**.
  Logout/reboot исключены из текущей работы прямым указанием пользователя;
  не выполнялись и не объявляются проверенными.
- Полный regression после исправления остановки systemd: **142 tests, 187.795 s,
  OK (skipped=5)**. Отдельный Docker
  smoke с проверкой отсутствия контейнеров без дополнительной cleanup:
  **1 test, 9.191 s, OK**. Старый real inbox fixture получил свободный artifact
  port вместо конфликтовавшего со службой 34567; адресная проверка прошла.
- Live Drive: **1 test, 1317.257 s, OK** — полный FAIL → console → fix →
  PASS → exact pull через настоящий `rclone:`; remote root
  `rclone:gdi-acceptance/gdi-v2-cli-j3ryubbx` сохранён для проверки.
  Service CI цикл: **1 test, 1585.194 s, OK** — Docker FAIL → fix → PASS,
  проверенные artifacts/console и exact pull через установленную службу.
  Lifecycle проверки завершены (см. результаты ниже). Первый live Drive прогон
  остановился по локальному subprocess timeout 90 секунд; сетевые fixture лимиты
  увеличены до 900 секунд; повторный прогон завершён успешно.
- Service PASS `b9f87415005d42919cf457ac79e864ed` сохранён локально, служба
  принудительно остановлена всей control group в UPLOAD_PENDING и перезапущена.
  Результат доставлен в **PUBLISHED**; SHA256 `result.json` и `build.log` до/после
  совпадают, каждая matrix variant исполнилась один раз. Проверены payloads всех
  четырёх артефактов `v3-one`, `v3-two`, `v4-one`, `v4-two`: `file.txt = fixed`.
  Снимки: `/tmp/gdi-service-recovery-before.json` и `...-after.json`.
- По отдельному запросу измерена передача случайного файла **1 MB (1 000 000 bytes)**:
  upload **7.602 s** (0.1315 MB/s), download **6.368 s** (0.1570 MB/s). Размер и
  SHA256 совпали. Измерено полное время `rclone copyto` с API/process overhead;
  remote папка создана заранее, acceptance worker/client работали в фоне.
  Файл: `rclone:gdi-acceptance/transfer-benchmark-20261009T135608Z-e8e459be/random-1MB.bin`.
- Добавлены opt-in `ServiceV2CliTests` для полного цикла через установленную службу,
  штатного stop во время job и SIGKILL всей control group без rerun.
- Lifecycle прогон обнаружил две разные причины отказа: тестовый процесс не имел
  Docker-группы для финальной проверки containers; `KillMode=control-group`
  передавал SIGTERM также act и прерывал текущий CI. Unit исправлен на `mixed`,
  regression выше прошёл. Для stale groups user manager host override использует
  `Type=forking`, PIDFile и launcher через `sg docker`: отслеживаемый MainPID —
  Python, прямой потомок user manager. Промежуточный notify-wrapper не прошёл
  stop-проверку и заменён; acceptance TimeoutStopSec увеличен до 900 секунд.
  SIGKILL job `efc0f20ed72545829d090d85552f4b94`: INTERRUPTED/PUBLISHED,
  исполнение STARTED ровно один раз, Docker-owned containers отсутствуют.
  Штатная остановка на существующем fixture-репозитории:
  job `01fd9492c80344029ec8faa756ccd3cb` — **PASS/PUBLISHED, verified=true**.
  Stop занял **227.572 s** вместе с завершением CI и доставкой результатов.
  После start исполнение STARTED ровно одно, SHA256 build.log не изменился.
  Служба снова active/running/enabled, ExecMainStatus=0.
- Доступно около **270 GB**; текущий state/spool занимает **1.3 MB**, cache
  **73 MB**. Это оценка текущего host, не проверка длительной нагрузки;
  retention и лимиты остаются backlog.
- Замер обычного `gdi push/pull` для bundle около 1 MB ждёт предоставленного
  пользователем репозитория. Создание нового benchmark-репозитория отменено;
  22/26 минут выше относятся к integration acceptance самого gdi, а не к CI
  обычного пользовательского проекта.
- Ссылки/manifest/CLI help согласованы с новым именем `INSTALL.md`; wheel/sdist
  повторно собраны и состав проверен.

Нижние исторические результаты исходного прогона сохранены; актуальный статус
Docker, Drive и службы приведён в этом продолжении. Логи и JSON доказательства,
включая неудачные попытки, сохранены в приватной папке
`~/.local/state/gdi/hatuncevk-acceptance/acceptance-2026-10-09/`.

### Согласованные решения

- Репозитории используют собственную `.gdi/config.json`, общую inbox и локальные
  lock/cache/CI state в `.gdi`; `.git/config` для настроек gdi не изменяется.
- Worker config v2 задаёт общий root, polling/retries/timeouts/runner без списка
  проектов и CI команд. CI выполняет workflows точного commit через act/Docker.
- Immutable notifications публикуются последними; маршрут сохраняется в ledger.
  Ready/result подтверждают точные bytes/identity; upload/recovery не повторяют CI.
- Обычный push уведомляет об истории без автоматического CI/fetch в пользовательские
  ветки. CI запускается явным запросом, pull выбранного PASS остаётся ff-only.
- **Не трогать `lora-sack/chatter`**; проверки нового runner только в отдельных fixtures.
- Drive Changes API, push notifications/PubSub, retention и multi-worker — backlog.

### Результат текущих 12 шагов

1. [x] Ревью изменений схемы v2 и связанных модулей. Проверены пример worker.json,
   CLI help/worker check, новый сквозной CLI v2 через отдельный процесс worker.
2. [x] CI state переносится atomic copy/fsync каждого файла; completion marker
   публикуется последним. Незавершённая миграция продолжается при наличии новой
   папки; outbox/job IDs сохраняются, конфликтующие копии не перезаписываются.
3. [x] Legacy capabilities fallback только при доказанном отсутствии общего файла.
   Ошибки сети/прав, повреждённый JSON и неверная схема передаются клиенту.
4. [x] Синтаксис worker/profile/selector проверяется до push. Для global worker
   наличие регулярных tracked YAML проверяется в выбранном Git commit до upload.
   Default profile `full` и сообщения CLI согласованы; legacy CLI продолжает работать.
5. [x] Добавлены v2 проверки pending revision/root, migration crashes, result fsync
   до ledger, receipt до ack, uncertain execution, missing workflow/secrets,
   symlinks, selector, timeout/SIGINT/SIGKILL и отсутствия rerun после restart.
6. [x] Реальный Docker runner: готовы отдельные fixtures matrix/needs/secrets,
   external actions и upload-artifact@v3/@v4, а также отдельный GitHub CI job.
   Docker CLI/daemon 29.9.0 установлены, служба active/enabled. Пользователь добавлен
   в `docker`; через `sg docker` локальный контейнер успешно запущен.
   Docker fixtures прошли в продолжении: 2 tests, OK. Загрузка images через
   настроенный daemon proxy работает.
7. [x] GitHub namespace извлекается из origin клиента, закрепляется в request v2,
   передаётся в event/repository/owner contexts и сохраняется при retry. Credentials
   и произвольный origin URL не передаются, per-repository host config не требуется.
8. [x] act_version закреплён (default 0.2.89); revision включает фактический SHA256
   act, host actrc hashes, Docker daemon/version и immutable local base image IDs.
   Worker не скачивает инструменты/images; нужен предварительный docker pull.
   Добавлены `worker check --runtime` и проверяемый `artifacts/environment.json`.
   Документированы границы host post-check при `--bind=false` и внешних YAML images/actions.
9. [x] Docker ownership/cleanup реализованы и покрыты unit tests: перед запуском
   сохраняются daemon identity и уникальный checkout, после CI/restart удаляются
   только act containers данного checkout. Actual Docker SIGKILL/restart smoke test
   прошёл. Volumes/networks и произвольный Docker из workflow не очищаются.
10. [x] GitHub CI устанавливает rclone/act и включает реальный local v2 CLI;
    отдельный job включает Docker acceptance. YAML разобран настоящим act --list.
    Wheel/sdist собраны локально; workflow/docs/fixtures/runner присутствуют в sdist,
    модуль runner присутствует в wheel.
    Публикации/runs на GitHub не было.
11. [x] Исправлены устаревшие пути cache/CI state, настройки Git config и legacy
    registered repository/queue wording. README/Install/worker/CI protocol/workflow,
    ARCHITECTURE, gdi.prompt/gdi.project согласованы. Добавлен docs/acceptance.md.
12. [x] Полный локальный и Docker прогоны успешны; user service установлена,
    active/enabled, linger включён. Настоящие Drive/service CI, SIGKILL recovery,
    штатный stop/start без rerun и текущая ёмкость диска проверены.
    Logout/reboot исключены пользователем и не выполнялись.

### Что осталось для завершения 12 шагов

1. [x] Пользователь `hatuncevk` добавлен в группу `docker`; проверены клиент/сервер
   29.9.0 и запуск контейнера из временного локального образа без сети через
   `sg docker`. Проверочный контейнер и образ удалены. Текущий процесс Codex ещё
   имеет старый список групп: для обычного запуска без `sg` перезапустить сессию.
   `hello-world` пока не загрузился из-за DNS, а не из-за прав.
2. [x] Загрузить `catthehacker/ubuntu:act-latest` и выполнить две opt-in проверки
   `DockerV2CliTests` по [docs/acceptance.md](docs/acceptance.md). Подтвердить
   matrix/needs/secrets, checkout, upload-artifact@v3/@v4, exact pull, а также
   SIGKILL/restart и очистку принадлежащих job контейнеров — закрыть пункты 6 и 9.
   Образ загружен, обе проверки прошли, пункты 6 и 9 закрыты. Daemon proxy настроен,
   hello-world скачан и запущен. Host DNS без серверов не блокирует pull через proxy.
3. [x] act v0.2.89 установлен в `/home/hatuncevk/.local/bin/act`.
   Проверенный образ закреплён по digest, `worker check --runtime` прошёл.
4. [x] В разрешённой отдельной папке `rclone:gdi-acceptance`
   выполнен реальный Drive цикл FAIL → console → fix → PASS → exact pull.
   Доставка artifacts/result проверена, в acceptance-службе подтверждён SIGKILL/
   restart во время UPLOAD_PENDING: исходные result/log bytes доставлены без rerun.
5. [x] Постоянный config и user service созданы; start/status подтверждены.
   PATH, rclone config, proxy и journal проверены; полный CI цикл службы прошёл.
6. [x] С владельцем host проверить остановку службы во время job, restart без
   rerun и выбранную политику linger; оценить место под spool/cache/logs.
   Logout/reboot исключены пользователем из текущей работы.
   Результаты обновлены, пункт 12 закрыт в согласованном объёме.

Запуск подготовленного GitHub workflow и commit/push текущих изменений ещё не
выполнялись; это отдельные действия, если требуется публикация результата.

### Проверки исходной сессии (до продолжения выше)

- Настоящий rclone **v1.75.1**, Git **2.43.0**, Python **3.12.3**.
- act **v0.2.89** скачан только в `/tmp/gdi-tools/act` без системной установки.
- Runner/inbox unit checks: **27 tests, 15.233 s, OK** (до последних дополнительных checks).
- Реальный CLI v2 (rclone local + act self-hosted): **4 tests, 45.515 s, OK**.
  FAIL → fix → PASS/exact pull, timeout, штатный SIGINT и SIGKILL/restart без rerun.
- Полный прогон: **139 tests, 188.410 s, OK (skipped=2)**. Пропущены только
  opt-in проверки реального Docker:
  `GDI_TEST_ACT=/tmp/gdi-tools/act PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q`.
  Логи fault injection с ERROR ожидаемы; итог unittest успешный.
- Wheel/sdist успешно собраны через временный build venv; проверено наличие
  `.github/workflows/ci.yml`, docs/acceptance.md, примера config, нового runner
  и fixtures в sdist, runner в wheel. `git diff --check` проходит.
- Доступ настроенного `rclone:` к Google Drive подтверждён read-only `rclone about`.
  Это не подтверждает CI/Drive upload/recovery или Docker artifacts.
- Docker установлен владельцем host после полного локального прогона. Повторная
  проверка 2026-10-09: CLI/server **29.9.0**, Linux/amd64; `docker.service` **active/enabled**,
  сокет `/var/run/docker.sock` принадлежит `root:docker`, права `srw-rw----`.
  Пользователь `hatuncevk` уже входит в `docker`, но текущий процесс Codex сохраняет
  старые группы. Через `sg docker` доступ подтверждён и успешно запущен временный
  контейнер с локальной статической программой: `gdi Docker local smoke: OK`.
  Проверочный контейнер/образ удалены; сеть и настройки службы не изменялись.
  `hello-world`/Docker fixtures блокирует DNS при загрузке image; daemon proxy пуст,
  через proxy окружения CLI Docker Hub доступен. Реальные workflows ещё не проверены.
- `~/.config/gdi/worker.json` и установленной gdi user service не создавалось;
  logout/reboot/linger не изменялись. Chatter не изменялся.

Команды и границы реальных проверок: [docs/acceptance.md](docs/acceptance.md).
Docker установлен пользователем. В продолжении по разрешению владельца установлена
gdi user service, включён linger и выполняются реальные Drive CI проверки.
Commit остаётся отдельным действием.
Inbox listing не гарантирует FIFO; notifications не решают конкуренцию Git push.
Ниже `[x]` означает наличие кода/указанных проверок, а не прохождение всех
эксплуатационных пунктов или полную совместимость act с любым GitHub workflow.

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

- [x] Проверить отдельные workflows fixtures через Docker, включая actions/upload-artifact v3/v4.
  Произвольные реальные проектные workflows требуют своих отдельных проверок.
- [x] Docker CLI/server 29.9.0, служба active/enabled; пользователь добавлен в
  `docker`, локальный запуск контейнера через `sg docker` успешен.
- [x] Настроить сетевой доступ Docker daemon к registry, установить act в постоянный
  путь и проверить полный выбранный CI.
- [x] Прогнать тот же цикл на настоящем Google Drive/rclone OAuth в разрешённом окружении.
- [x] Установить user service, проверить PATH/rclone config и linger на конкретном host.
  Logout/reboot исключены пользователем и не проверялись.
- [x] Проверить принудительную остановку с Docker acceptance CI и его процессами.
  Recovery во время исполнения и доставки прошёл без rerun; reboot исключён.
  Полный CI пользовательского проекта требует предоставленного репозитория.
- [ ] Для длительной нагрузки определить доступное место под локальный spool и remote CI logs.

Наличие локального интеграционного теста не объявляет эти эксплуатационные проверки
выполненными. gdi не меняет credentials, linger и службы автоматически при установке пакета.

## Дальнейшее развитие

- [ ] Первым этапом, до оптимизации, добавить видимый прогресс длительных CLI команд: текущая операция и время ожидания, включая обращения к rclone/Drive.
- [ ] Добавить профилирование таймингов команд: общее время и длительность отдельных этапов/вызовов rclone; по замерам определить узкие места перед оптимизацией. При подключении `lora-sack-protocol` команда `remote add --init` заняла около 40 секунд без промежуточного вывода (наблюдение пользователя).
- [ ] После добавления прогресса и профилирования уменьшить число обращений к Drive и передач служебных данных в workflow обмена/CI, особенно при повторном `push` без новых commits; сохранить проверки целостности, обнаружение конфликтов и восстановление после прерывания. Наблюдения пользователя на `lora-sack-protocol`: первый push полного bundle 1 349 293 bytes — около минуты; повторный `Already published` — около 30 секунд, отдельная передача файла сопоставимого размера — около 7 секунд. Сравнить число вызовов rclone и тайминги до/после оптимизации.
- [ ] Явно показывать подключение и выбранную ветку в выводе `push`, `fetch` и `pull`, включая `Already published`; при отсутствии аргумента ветки показывать фактически выбранную текущую ветку (например, `dev_chat_binary`).
- [ ] Добавить команду `gdi status`: показывать текущую Git-ветку и HEAD, подключения GDI с URL/Repository ID и состояние публикации текущей ветки.
- [ ] Сделать ветки на Drive понятными человеку: показывать читаемые имена/указатели веток внутри общей папки репозитория, а не только хеши каталогов публикаций. Сохранить один URL и Repository ID на репозиторий, отдельные цепочки публикаций веток и продумать совместимость с существующей структурой remote.
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
