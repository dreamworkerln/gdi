# gdi 0.3.0 (pre-alpha)

Обмен Git-историей через Google Drive и настроенный rclone: инкрементальные bundles,
проверка metadata и SHA256, fetch в отдельные refs, pull только fast-forward.
Постоянный worker опрашивает общую inbox всех проектов и выполняет GitHub Actions
workflows из точного commit через act + Docker; агент
получает progress, консоль и проверенный PASS/FAIL и повторяет исправления автономно.
Работает на Linux с Python 3.10+, включая 3.10.12. Python runtime dependencies нет.

Полная установка, настройка Bash, Google Cloud OAuth и rclone: **[INSTALL.md](INSTALL.md)**.
Формат данных и гарантии: [docs/protocol.md](docs/protocol.md).
CI через обычный CLI с rclone и проверка на host пользователя:
[docs/ci.md](docs/ci.md).
Короткий справочник человеку: **[QUICKSTART.md](QUICKSTART.md)**.
Общая очередь и подключения `.gdi`: [docs/inbox.md](docs/inbox.md).
Worker/systemd: [docs/worker.md](docs/worker.md). Архитектура: [ARCHITECTURE.md](ARCHITECTURE.md),
состояние проверок и дальнейшие задачи: [TODO.md](TODO.md).
Профилирование времени команд и журнал всех вызовов rclone: [docs/profiling.md](docs/profiling.md).

Агент с Drive-коннектором запускает `python3 -m gdi agent ...` из исходников
без rclone и сторонних Python-пакетов: [docs/agent.md](docs/agent.md).

На Linux CLI и worker используют временные процессы native rclone RC с ограничением
жизни операцией, свежей проверкой repository ID и остановкой при гибели родителя.
`GDI_TRANSPORT=cli` выбирает прежний транспорт для диагностики; форматы данных сохранены.

```bash
# В первом локальном Git-репозитории; отдельная пустая папка на Drive.
gdi remote add drive gdrive:gdi/my-project --init
gdi push

# В другом clone того же проекта; --init здесь уже не нужен.
gdi remote add drive gdrive:gdi/my-project
gdi fetch
git log --oneline HEAD..refs/remotes/drive/main
gdi pull
```

`drive` — локальное имя подключения gdi; `gdrive` — имя remote в rclone.
`main` в примере замените на имя вашей ветки. Для нового проекта используйте отдельную
папку, например `gdrive:gdi/another-project`.

## CLI

| Команда | Поведение |
| --- | --- |
| `gdi remote add NAME URL --init` | Один раз создаёт протокол в пустой папке, сохраняет repository ID локально |
| `gdi remote add NAME URL [--repository-id ID]` | Подключает существующий remote; без ID доверяет идентичности при первом подключении |
| `gdi remote default NAME` | Выбирает подключение для коротких команд |
| `gdi log [NAME] [BRANCH] [-n N] [--json]` | Проверенная история публикаций ветки, новые первыми; по умолчанию 20 записей |
| `gdi status [NAME] [--json]` | Ветка, HEAD, worktree, подключения и свежий статус публикации без скачивания bundles |
| `gdi remote list` | Показывает имена, адреса и закреплённые repository IDs |
| `gdi remote remove NAME` | Удаляет локальные настройки, оставляет удалённые данные и fetched refs |
| `gdi push [NAME] [BRANCH]` | Первый bundle полный, следующие инкрементальные; повтор того же HEAD ничего не публикует |
| `gdi push [NAME] [BRANCH] --full` | Создаёт полный контрольный bundle, в том числе для уже опубликованного HEAD дельты |
| `gdi push [NAME] [BRANCH] --checkpoint-every N` | Полный bundle после каждых N обновлений от предыдущего полного (по умолчанию 20) |
| `gdi push [NAME] --ci --worker ID [--profile full] [--json]` | Публикует commit и запрос CI в общую inbox |
| `gdi ci status/wait/logs NAME JOB_ID` | Прогресс, streaming console и проверенный terminal result |
| `gdi ci retry NAME JOB_ID [--json]` | Явный повтор завершённой проверки, новый job ID |
| `gdi pull [NAME] --passed --job ID --profile full` | Fast-forward именно на SHA выбранного проверенного PASS |
| `gdi worker check/run/install --config PATH` | Проверка config, foreground worker, явная установка user service |
| `gdi worker start/restart/status/stop` | Управление постоянной systemd user service; restart перечитывает config и код |
| `gdi worker logs [-f] [-n N] [-b]` | Последние 100 сообщений службы; follow, число строк, текущая загрузка |
| `gdi fetch [NAME] [BRANCH]` | Восстанавливает недостающую историю, обновляет `refs/remotes/NAME/BRANCH` |
| `gdi pull [NAME]` | Получает текущую ветку и применяет только fast-forward |
| `gdi cache clear NAME` | Удаляет только локальный кеш проверенных объектов этого repository ID |
| `gdi gc NAME [--keep-checkpoints N]` | Показывает план очистки bundles всех веток; по умолчанию сохраняет два полных checkpoint и дельты после них |
| `gdi gc NAME --apply --quiescent` | Проверяет сохраняемую историю с нуля, повторно сверяет remote и удаляет устаревшие bundles; требует остановки обмена на всех машинах |
| `gdi -v`, `gdi --version` | Одинаковый вывод: версия и золотая ASCII-эмблема из C&C без надписей |
| `gdi -h`, `gdi --help` | Одинаковая справка с примерами использования |

Первое подключение выбирается по умолчанию; смена — `gdi remote default NAME`.
`gdi push`, `gdi fetch`, `gdi pull`, `gdi log` показывают подключение и выбранную ветку.
Каталоги на Drive читаемы: `branches/dev/`, `branches/feature%2Flogin/`.
По умолчанию push/fetch работают с текущей веткой; pull всегда работает с текущей.
Справка и версия работают вне repo. Цвет эмблемы включается в терминале,
`FORCE_COLOR=1` включает его в pipe, `NO_COLOR` отключает. В выводе версии нет справки;
эмблема повторяет силуэт из `gdi.png` и не содержит текстовых подписей.
Справка содержит действующие команды и примеры с выровненными комментариями.
Примеры CI и точный протокол: [docs/ci-protocol.md](docs/ci-protocol.md).
Fetch разрешён при незакоммиченных изменениях; pull требует чистое дерево, включая
untracked files. Push передаёт только commits и не создаёт commit автоматически.
Успех — exit 0, ошибка — 1, неверные аргументы — 2, прерывание Ctrl+C — 130.
Для `ci wait`: PASS → 0, другой terminal outcome → 1, timeout ожидания → 124.
Timeout/Ctrl+C клиента не отменяют job. JSON stdout отделён от progress stderr.

Подключения gdi хранятся в `.gdi/config.json`; `.git/config` не изменяется.
Общий inbox root по умолчанию — родитель URL проекта; для вложенной структуры
задайте `remote add --inbox-root gdrive:gdi`. Добавьте `.gdi/` в `.gitignore`.

## Автономный CI

Владелец host один раз задаёт общий Drive root, polling/ретраи/таймауты в worker.json,
устанавливает act/Docker, проверяет foreground запуск и включает службу. Списка
репозиториев и команд CI в config version 2 нет. Пример: [examples/worker.json](examples/worker.json).

```bash
gdi worker check --config ~/.config/gdi/worker.json
gdi worker install --config ~/.config/gdi/worker.json
gdi worker start
```

Агент после одного или нескольких commits:

```bash
gdi push drive --ci --worker user-host --profile full --json
gdi ci wait drive JOB_ID --follow --timeout 3600 --json
gdi ci logs drive JOB_ID --output /tmp/ci-JOB_ID.log
```

При FAIL агент исправляет код, делает новый commit и повторяет. Worker хранит Git
cache, выполняет exact SHA в отдельном checkout, публикует stdout+stderr chunks,
полный build.log и result. После PASS/FAIL продолжает работать. Ошибка upload не
вызывает повторный CI; recovery неопределённого исполнения даёт INTERRUPTED.

Пользователь получает выбранный PASS в свой чистый clone:

```bash
gdi pull drive --passed --job JOB_ID --profile full
```

Диагностика worker: `gdi worker logs` показывает последние 100 сообщений журнала службы,
`gdi worker logs --follow` следит за новыми. `-n 200` меняет число сообщений,
`--boot` ограничивает текущей загрузкой системы. Ctrl+C завершает просмотр;
worker продолжает работать. Команда работает вне Git-репозитория и использует
`journalctl --user -u gdi-worker.service` без pager.
CI консоль: `gdi ci logs`/`wait --follow`. Команды CI берутся из `.github/workflows` проверяемого commit.
`--workflow`, `--event`, `--job`, `--input KEY=VALUE` выбирают проверку при submit.
Worker исполняет доверенные workflows; act имеет доступ к Docker Engine.

## Инкрементальный обмен

```text
full(A) → delta(A..B) → delta(B..C) → … → full(U) → delta(U..V)
```

База дельты — предыдущая публикация той же ветки. Metadata содержит точные base HEAD,
publication ID и фактические prerequisites из bundle. Пропущенные обновления получатель
догружает последовательно; новый клиент начинает с последнего полного bundle.
Обычно повторный push/fetch не скачивает уже проверенные bundles.

После 20 обновлений от полного bundle автоматически создаётся новый полный: при
значении 20 между полными бывает не более 19 дельт. `--checkpoint-every 1` всегда
публикует полный bundle; `--full` позволяет сделать контрольную публикацию вручную.
Большие новые файлы могут давать большие дельты.

Проверенные объекты сохраняются в `.gdi/cache/<repository-id>/repository.git` основного worktree.
Кеш общий для linked worktrees, не коммитится и удерживает принятые commits отдельными refs.
Неполное восстановление можно продолжить после перезапуска. Потерянный или обнаруженный
повреждённый кеш восстанавливается из remote; ручная очистка — `gdi cache clear NAME`.

**Протокол v3 несовместим с v1/v2.** Для старого remote заведите новую пустую папку и
один раз выполните `remote add --init`, затем подключите остальные клиенты к новому ID.
Local config v1 тоже требует пересоздания; новый config v2 содержит `default_remote`.
Старый remote автоматически не переписывается. Порядок перехода есть в INSTALL.md.

## Очистка bundles на Drive

```bash
gdi gc drive                       # только план и размеры
# Дождитесь доставки CI, остановите worker и обмен на ВСЕХ машинах, затем:
gdi gc drive --apply --quiescent
```

GC сохраняет по умолчанию два последних полных checkpoint каждой ветки, последующие
bundles и все дополнительные базы дельт. Вся metadata и bundles без manifest остаются.
Применение отказывает при legacy CI queue markers или незавершённых jobs v1/v2. CI logs/results
остаются; старым gdi 0.2.1 нельзя применять GC на CI remote.
Перед удалением все сохраняемые публикации проверяются в отдельных временных Git
репозиториях без локального кеша. Формат protocol v3 не меняется.

`--quiescent` — подтверждение остановки остальных клиентов, а не удалённая блокировка.
GC не запускается автоматически. Он удаляет лишние упаковки, сохраняя commits;
полные bundles по-прежнему растут вместе с историей. Для Google Drive rclone по
умолчанию отправляет удалённые файлы в Корзину. Подробности, частичные ошибки и политика
хранения: [docs/gc.md](docs/gc.md).

## Ограничения

- Только последовательные push в один ref. Конкурирующие продолжения выявляются как
  конфликт; распределённой блокировки и атомарного Git push нет.
- Обычные полные SHA-1 worktrees. Bare, shallow, partial clones, replace refs и grafts
  отвергаются. В linked worktrees gdi использует общую конфигурацию и общий локальный lock.
- При наличии submodules или LFS pointers в публикуемом commit операция отвергается.
  История передаётся как Git objects; внешние данные старых commits не переносятся.
- Push/fetch/pull работают с одной веткой на команду; GC проверяет все опубликованные
  ветки. Tags, удаление веток, force push, refspecs и clone не реализованы.
  Metadata, orphan bundles и локальный кеш накапливаются; фонового GC нет.
- Drive API/push notifications, отмена jobs и CI/local spool retention пока не реализованы.
  Общие immutable уведомления через rclone inbox уже поддерживаются.
  Работает один назначенный worker на remote; multi-worker координации нет.
- SHA256 проверяет целостность, а не авторство. Доступ к папке Drive предоставляйте
  доверенным участникам. Подписи публикаций и защита от удаления всей remote-истории
  не реализованы. Не выполняйте обычные Git-команды, меняющие worktree, одновременно с pull.

При ошибке fetch рабочая ветка и файлы не меняются. Отказ pull из-за расхождения может
оставить новый fetched ref для изучения; автоматического merge/rebase/reset нет.
При сбое загрузки bundle без manifest остаётся непринятым; повторите push после
восстановления сети. Подробности конфликтов и неполных manifest — в описании протокола.

## Разработка и проверки

Задайте `INSTALL_DIR` — абсолютный путь к папке с исходниками gdi.

```bash
INSTALL_DIR="/путь/к/gdi"
cd "$INSTALL_DIR"
python3 -m gdi --help
python3 -m unittest discover -s tests -v
```

Новые tests используют реальные временные Git repositories и fake transport; при наличии
rclone также проверяется настоящий CLI с local backend, без Google credentials.
Проверяются пропущенные дельты, checkpoints, повторные операции без загрузок, потеря
кеша, resume после ошибки, merge с несколькими prerequisites и повреждённые данные.
GC tests проверяют все ветки, старые базы дельт, восстановление без кеша, отказ при
повреждении/изменении remote и повтор после частичного удаления.
CI tests проверяют отдельный постоянный worker, FAIL → log → исправление → PASS,
точный pull, upload/restart recovery, timeout descendants, binary console, artifact
checksums и защиту GC. Для тестов достаточно стандартной библиотеки.
Версия act закрепляется полем `act_version` (по умолчанию `0.2.89`). Перед запуском
worker образы из `platforms` должны быть загружены через `docker pull`. Worker
включает фактический SHA256 act, Docker daemon/version и immutable image IDs
в execution revision; выбранные base images запускаются с `--pull=false`.
После обновления act/images перезапустите worker. Отчёт сохраняется в проверяемом
`artifacts/environment.json`. Workflow actions и собственные container images
закрепляются внутри самого YAML. Команда `worker check --runtime` проверяет эту среду.

Сквозные CLI fixtures, Docker/artifact и signal/restart проверки:
[docs/acceptance.md](docs/acceptance.md).
Реальный Google Drive и установка службы на host требуют отдельной проверки в
разрешённом окружении; тесты rclone local не подменяют её.

Лицензия: [GPLv3](LICENSE).
