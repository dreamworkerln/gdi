# TODO и состояние gdi 0.3

Путь к исходникам в этом документе обозначается `$INSTALL_DIR`, домашняя папка —
`$HOME`. Для команд из исходников задайте свой абсолютный путь:

```bash
INSTALL_DIR="/путь/к/gdi"
cd "$INSTALL_DIR"
```

## Текущие изменения и проверки — 2026-10-09

Работа ведётся в `$INSTALL_DIR`, ветка `dev`, HEAD
`91e7e801ad74bf9e643cbd12825f7f326b7b5de6`. Перечисленные ниже изменения находятся
в рабочем дереве; отдельный Git commit/push этих исправлений не выполнялся.
Публикации через gdi на настоящем Drive выполнены и проверены.

### Сделано

- [x] Документация очищена от абсолютных путей к checkout на конкретной машине
  и привязок к стороннему проекту. В блоках команд с исходниками задан
  `INSTALL_DIR="/путь/к/gdi"`, домашняя папка обозначается `$HOME`;
  примеры используют абстрактный `my-project`.
- [x] Добавлен базовый прогресс CLI: этапы операции автоматически в терминале,
  подробные вызовы rclone и время ожидания каждые 2 секунды — при профилировании.
  Поддержаны `--progress`, `--no-progress`, `GDI_PROGRESS`, вывод на stderr.
- [x] Обычный `gdi push` в новом терминале больше не печатает тайминги и счётчики.
  Подробности и итог замера включаются только через --profile-log/GDI_PROFILE_LOG;
  обычный прогресс показывает этапы, --no-progress полностью убирает их.
- [x] Добавлены `--profile-log PATH` и `GDI_PROFILE_LOG`: журнал JSONL с ID запуска,
  общим временем команды, длительностью этапов, всеми запусками rclone, аргументами,
  exit codes, ошибками и итоговыми счётчиками. Git subprocess также измеряются;
  содержимое stdout/stderr в журнал не записывается. Начало вызова сохраняется
  до запуска процесса; JSON stdout CLI остаётся пригодным для автоматизации.
- [x] Создана инструкция [docs/profiling.md](docs/profiling.md), добавлены ссылки
  из README, INSTALL и QUICKSTART.
- [x] Текущий Git-репозиторий подключён как `drive` к `gdrive:gdi/gdi`;
  Общий inbox root: `gdrive:gdi`. Первоначальный ID `d5619450692e445f9efcfb08bb45018b`
  относится к удалённому пользователем v2 remote. Новый v3 ID —
  `89b39ff8382347d5b49f7af2f6903836`; remote пересоздан через `--init`.
- [x] Убраны отдельные rclone mkdir перед upload manifest и inbox notification:
  транспорт сам создаёт родительские папки при загрузке.
- [x] Добавлены локальные записи успешной доставки обычного notification в
  `.gdi/notifications/` через atomic write/fsync. Повторный push той же публикации
  пропускает уже выполненный upload; ошибка upload или crash до записи оставляют
  возможность повторить доставку того же immutable event. CI outbox/retry сохраняются.
- [x] Несколько manifests выбранной ветки читаются одним процессом rclone через
  `copy --files-from-raw`. Каждый снимок загружается в новую временную папку;
  проверки SHA256, схемы, цепочки и конкурирующих публикаций сохранены.
- [x] Исправлены immutable uploads: single-file `copyto --immutable` на проверенном
  rclone 1.75.1 local backend перезаписывал изменённый файл. Загрузка переведена
  на directory copy с одним файлом и `--immutable --checksum --no-traverse`;
  отличающиеся bytes отвергаются даже при одинаковых размере и mtime.
- [x] Добавлены 9 проверок в `tests/test_performance.py`: повтор после потребления
  notification, failed upload, crash до receipt, corruption при повторном чтении,
  batch freshness, missing/oversized metadata, immutable overwrite,
  профилирование ошибок и KeyboardInterrupt.

### Короткие команды и новый формат веток

- [x] `gdi push`, `gdi fetch`, `gdi pull` без обязательного имени подключения;
  текущая ветка используется по умолчанию. Первое подключение выбирается default,
  смена — `gdi remote default NAME`. Явные `REMOTE [BRANCH]` сохранены; при нескольких
  подключениях без default команда просит выбрать подключение.
- [x] `gdi status [REMOTE] [--json]`: текущая ветка, HEAD, dirty worktree,
  подключения с URL/Repository ID/default и свежий статус committed HEAD.
  Состояния: unpublished/published/ahead/behind/diverged/unknown/detached/error.
  Для определения ancestry неизвестного remote commit предлагается fetch;
  status сам не скачивает bundles и не обновляет Git refs.
- [x] `changes present` в human status выделяется красным в терминале;
  поддержаны NO_COLOR/FORCE_COLOR, обычный pipe и JSON остаются без ANSI.
- [x] Push/fetch/pull показывают выбранные подключение, URL и ветку, включая
  unchanged push. JSON push содержит remote/url/branch без текстовых примесей.
- [x] Git exchange protocol v3: авторитетные immutable manifests находятся в
  `branches/<encoded-branch>/<publication-id>.json`. `dev` читается как `dev`,
  `feature/login` как `feature%2Flogin`; `%` кодируется `%25`, Unicode сохраняется.
  Отдельные цепочки и строгие проверки сохранены в exchange, CI reader, batch и GC.
- [x] Обратная совместимость с Git exchange v1/v2 и local config v1 удалена
  по явному указанию пользователя. Local config v2 содержит default_remote;
  legacy Git config не импортируется. Инструкция пересоздания — INSTALL.md.
  Для будущих breaking changes необходимость совместимости спрашивать заранее;
  предпочтение закреплено в AGENTS.md.
- [x] Добавлены 7 проверок коротких команд, default/ambiguity, состояний status,
  отсутствия загрузок/изменений refs при status, старых форматов, имён веток и GC.
  Новый формат: exchange/GC/performance — 90 tests, 61.540 с, OK;
  short CLI — 7 tests, 1.864 с, OK. Настоящий rclone local batch отдельно проверен
  с Unicode, `%2F` и `%252F`: имена различаются и читаются без потерь.
- [x] После удаления папок пользователем `gdrive:gdi/gdi` пересоздан в v3:
  init — 31.865 с / 6 вызовов; короткий push ветки dev — 51.376 с / 8 вызовов,
  опубликован committed HEAD `91e7e801ad74bf9e643cbd12825f7f326b7b5de6`.
  Publication: `087f65d2d51597c9dba418f5117f60113d545131ef8ef3f3a090d9167162d95c`.
  Профиль нового remote — `/tmp/gdi-v3-live.jsonl`. Рабочие изменения ещё не закоммичены.

- [x] Финальный regression нового формата: **158 tests, 181.109 с, OK (skipped=10)**.
- [x] Настоящий v3 Drive smoke — **PASS**: short fetch в пустом Git repo сохраняет
  unborn HEAD и ставит точный tracking ref; short pull получает опубликованный commit;
  свежий status меняется behind → published. Fetch — 23.037 с / 4 вызова,
  pull с cache — 14.297 с / 3 вызова. Исходный worktree не менялся.
- [x] Новый unchanged `gdi push --json` — 16.874 с / 3 вызова; created=false,
  remote=drive, branch=dev, тот же Publication ID, без повторной доставки notification.
  Новый source status — 17.949 с / 3 вызова: HEAD опубликован, worktree dirty.
  Evidence: `/tmp/gdi-v3-regression.log`, `/tmp/gdi-v3-live.jsonl`,
  `/tmp/gdi-v3-repeat.jsonl`, `/tmp/gdi-v3-status.jsonl`, `/tmp/gdi-v3-receiver-*`.

### Подтверждённые результаты

| Операция | До оптимизации | После оптимизации |
| --- | --- | --- |
| Создание remote | 26.146 с / 6 вызовов rclone | Повторного замера создания нет |
| Full push | 67.473 с / 10 вызовов | 44.861 с / 8 вызовов |
| Unchanged push | 29.630 с / 5 вызовов | 15.261 с / 3 вызова |
| Incremental push | Сравнимого исходного замера нет | 55.331 с / 10 вызовов |

Full push после изменений проверен в отдельной acceptance-ветке с тем же исходным
commit; имя ref и размер bundle немного отличаются (266 295 → 266 319 bytes).
Времена относятся к отдельным прогонам при изменчивых сетевых задержках.

- [x] Полный regression: **151 tests, 167.106 с, OK (skipped=10)**.
  Пропущены opt-in act/Docker/Drive/service tests без environment flags.
- [x] Отдельный настоящий Drive full → incremental → unchanged → fetch → pull:
  **PASS**. Проверены JSON stdout, publication ID, точный HEAD и bytes файла;
  fetch оставляет HEAD пустого checkout нетронутым, pull применяет нужный commit.
  С двумя manifests unchanged push занял 13.534 с / 3 вызова;
  fetch — 24.295 с / 5 вызовов, pull с verified cache — 11.601 с / 3 вызова.
- [x] Bash-блоки с INSTALL_DIR и запуск CLI из checkout с пробелами проверены
  при чистке документации; `git diff --check` проходит.

Evidence: `/tmp/gdi-regression-after-optimization.log`,
`/tmp/gdi-drive-after-optimization.jsonl`,
`/tmp/gdi-drive-smoke/gdi-acceptance-9749373b2510/`.
Acceptance-ветка `gdi-acceptance-9749373b2510` была сохранена в старом v2 remote;
позднее пользователь удалил эти папки перед пересозданием v3. Локальный evidence остался.
Подробности и границы замеров: [docs/profiling.md](docs/profiling.md).

### Ещё не сделано

Открытые задачи перечислены в разделе «Дальнейшее развитие»: полный прогресс
передачи с объёмом/процентом/скоростью, `gdi log`, дальнейшая оптимизация metadata/API
и backlog CI. Короткие push/fetch/pull, status и читаемые ветки уже сделаны.
Базовый вывод операций и времени ожидания не закрывает задачу прогресса передачи.

## История завершения 12 шагов — 2026-10-09

По запросу пользователя возобновлены текущие 12 шагов. Раздел «Дальнейшее развитие»
остаётся backlog. Репозиторий: `$INSTALL_DIR`, ветка `dev`,
исходный HEAD `4cbd6ac` (`refactored`). Прежний handoff про незакоммиченную схему на
`29f4baa` устарел: схема v2 уже находится в `4cbd6ac`.
На момент этой исторической сессии исправления были в рабочем дереве;
commit и публикация проекта не выполнялись. Текущий статус приведён выше.

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
- act v0.2.89 установлен в `$HOME/.local/bin/act`; SHA256
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
- Проверки runner выполняются только в отдельных fixtures.
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
3. [x] act v0.2.89 установлен в `$HOME/.local/bin/act`.
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
  logout/reboot/linger не изменялись.

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

- [x] Первым этапом, до оптимизации, добавить видимый прогресс длительных CLI команд: текущая операция и время ожидания, включая обращения к rclone/Drive. Добавлены `--progress`/`--no-progress`, автоматический вывод этапов в терминале; подробности и время ожидания rclone каждые 2 секунды включаются при профилировании.
- [x] Добавить профилирование таймингов команд: общее время и длительность отдельных этапов/вызовов rclone. Добавлены `--profile-log PATH`/`GDI_PROFILE_LOG`: JSONL с каждым вызовом rclone, аргументами, этапом, длительностью, exit code и итоговыми счётчиками; инструкция — [docs/profiling.md](docs/profiling.md). При подключении пользовательского репозитория команда `remote add --init` заняла около 40 секунд без промежуточного вывода (наблюдение пользователя).
- [x] По журналу профилирования подключения и push самого gdi через настоящий Google Drive определить узкие места. Замеры пользователя: init 26.146 с / 6 вызовов rclone; первый push 67.473 с / 10 вызовов (bundle 266 295 bytes, его upload 6.507 с); повторный push 29.630 с / 5 вызовов. Практически всё время уходит на rclone, включая повторную доставку notification и последовательные чтения metadata. Подробности — [docs/profiling.md](docs/profiling.md).
- [x] Проверить первый этап оптимизации на том же Drive: убраны отдельные mkdir перед upload manifest/notification, добавлена durable запись доставки обычного notification и пакетное чтение manifests выбранной ветки. Первый повтор после обновления: 19.599 с / 4 вызова (создание записи доставки), следующий: 15.261 с / 3 вызова вместо 29.630 с / 5. Полный push в отдельной acceptance-ветке: 44.861 с / 8 вызовов вместо исходных 67.473 с / 10. Identity/свежие chain checks сохранены. Сквозной Drive full/incremental/unchanged/fetch/pull — PASS; regression — 151 tests, 167.106 с, OK (skipped=10). Immutable uploads переведены на directory copy с --immutable --checksum: подтверждён отказ перезаписи при совпадающих размере/mtime. Подробности и границы замеров — [docs/profiling.md](docs/profiling.md).
- [ ] Добавить понятное отображение прогресса обычных `gdi push` и `gdi pull`: текущий этап, переданные bytes/общий размер и процент, когда объём известен, скорость и время ожидания. Показывать прогресс в терминале без включения профилирования; сохранять чистый JSON stdout. Уже реализованный вывод операций rclone и времени ожидания — базовый этап, прогресс самой передачи ещё требуется.
- [x] Добавить краткие `gdi push`, `gdi fetch`, `gdi pull`: default connection, текущая ветка, явный REMOTE/BRANCH, `gdi remote default NAME`.
- [ ] Добавить `gdi log` с краткой историей текущей ветки и выбором подключения по тем же правилам.
- [ ] Продолжить сокращение metadata/API операций на основе новых профилей: обычный unchanged push всё ещё читает identity, listing и manifests; incremental push в acceptance занял 55.331 с / 10 вызовов (сравнимого исходного Drive-замера нет). Проверить дальнейшее переиспользование соединений/metadata и влияние длины цепочки без ослабления SHA256, обнаружения конфликтов и recovery. Исторические наблюдения пользователя: bundle 1 349 293 bytes — около минуты, `Already published` — около 30 секунд, отдельная передача файла — около 7 секунд.
- [x] Показывать подключение, URL и выбранную ветку в push/fetch/pull, включая Already published; JSON push содержит remote/url/branch.
- [x] `gdi status [REMOTE] [--json]`: ветка, HEAD, worktree, URL/Repository ID/default и свежие состояния публикации без bundle download.
- [x] Читаемые ветки на Drive через `branches/<encoded-branch>/` в Git protocol v3; один URL/Repository ID и отдельные цепочки. Обратная совместимость удалена по указанию пользователя; remote пересоздан.
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
