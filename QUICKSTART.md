# gdi: краткий справочник для человека

gdi переносит commits через Google Drive и запускает CI на вашем компьютере.
Агент получает прогресс, консоль и результат, исправляет ошибку и отправляет новый
commit. Ваш рабочий каталог при этом не меняется.

Полная первоначальная настройка Python, rclone, Google OAuth и worker:
[INSTALL.md](INSTALL.md). Здесь предполагается, что она уже выполнена.
`drive` — подключение gdi, `gdrive:` — подключение rclone, `user-host` — ID worker,
`full` — общее имя исполнения workflows; команды берутся из `.github/workflows`
проверяемого commit. Замените имена и пути в примерах своими.

Для измерения задержек: `gdi push drive --profile-log PATH --progress`.
Журнал содержит каждый вызов rclone, этапы и итоговое время;
подробности — [docs/profiling.md](docs/profiling.md).
Прогресс этапов и передачи байтов/процента/скорости доступен без профилирования;
`--progress` включает его, `--no-progress` отключает.

## Один раз для проекта

На первой машине, из Git-репозитория:

```bash
gdi remote add drive gdrive:gdi/my-project --init
gdi remote list
```

Сохраните выведенный Repository ID. На другой машине, из clone того же проекта:

```bash
gdi remote add drive gdrive:gdi/my-project --repository-id REPOSITORY_ID
```

`--init` нужен только один раз для отдельной пустой папки. Подключение хранится в
`.gdi/config.json`; добавьте `.gdi/` в `.gitignore`. По умолчанию общий inbox root —
родитель URL проекта (`gdrive:gdi`); для вложенных путей есть `--inbox-root`.

## Один раз для CI host

На компьютере, который будет выполнять CI, настройте `~/.config/gdi/worker.json`
по [примеру](examples/worker.json): `config_version: 2`, `worker_id: "user-host"`,
общий `remote_url: "gdrive:gdi"`. Новые проекты и ветки не требуют изменения config.
Установите act **v0.2.89** и Docker по [INSTALL.md](INSTALL.md); для act и Docker
в config можно указать абсолютные пути. Пользователь службы должен иметь доступ
к Docker, а rclone — к указанному Drive root.

Загрузите образ из `platforms` и проверьте среду до установки службы:

```bash
docker version
docker pull catthehacker/ubuntu:act-latest
rclone lsf gdrive:gdi
gdi worker check --config ~/.config/gdi/worker.json --runtime --json
```

Если в config выбран другой образ, загрузите его. Проверенный образ можно закрепить
по digest в `platforms`. Worker использует уже загруженные base images; после
обновления act, образа или config перезапустите службу.

Установите и запустите user service:

```bash
gdi worker install --config ~/.config/gdi/worker.json
gdi worker start
gdi worker status
systemctl --user status gdi-worker.service
```

Установка не запускает службу до `worker start`; start также включает её автозапуск
при старте user manager. Служба не читает `.bashrc`: PATH, proxy, secrets и нестандартный
`RCLONE_CONFIG` настраиваются через unit override/EnvironmentFile.
Подробности и работа после выхода из аккаунта через linger: [docs/worker.md](docs/worker.md).

Каждый host общего root должен иметь **свой worker_id**. Два host с одинаковым ID
не защищены локальным lock от одновременного исполнения. Каждый запрос адресуется
одному worker, выбранному явно или по свежему heartbeat/совместимости. Общая inbox опрашивается
без обхода папок репозиториев.

Фактический ID host показывает `gdi worker check --json`, из
`~/.config/gdi/worker.json`; для другого пути добавьте `--config PATH`.
Это проверка config, состояние службы смотрите через `gdi worker status`.
Заменяйте `user-host` в CI-примерах этим ID. В простое старые capabilities и
READY на Drive не доказывают, что компьютер сейчас включён.

Для переносимого CI можно опустить `--worker`; `gdi ci workers drive --json`
покажет кандидатов. Выбор по загрузке, round robin, закрепление host и автоматические
таймауты описаны в [docs/scheduling.md](docs/scheduling.md). После отправки:

```bash
gdi ci supervise drive JOB_ID --policy examples/ci-portable.json --watch --json
```

Пример policy разрешает повторяемый ubuntu-latest CI, включая одновременное исполнение
на потерявшем сеть A и заменяющем B. Для устройств/побочных эффектов нужна своя политика.

## Передать ветку без CI

На отправителе, после обычного Git commit:

```bash
gdi push
```

На получателе, в clone проекта на той же ветке:

```bash
gdi fetch
git log --oneline HEAD..refs/remotes/drive/main
gdi pull
```

Замените `main` именем своей ветки. Push/fetch по умолчанию используют текущую ветку;
для другой можно указать её явно, например `gdi push drive feature`.
Fetch обновляет отдельный remote ref, pull делает fast-forward текущей ветки и требует
чистого рабочего дерева. Обычный push уведомляет worker об истории, CI запускается
через `--ci`.
Если pull не меняет HEAD, команда выводит `Already up to date.` после проверки Drive.

## Подключение по умолчанию и статус

Первое подключение автоматически выбирается для коротких команд `gdi push`,
`gdi fetch`, `gdi pull`, `gdi log`. Каждая команда показывает подключение, URL и выбранную ветку.
При нескольких подключениях используйте сохранённое default либо укажите имя явно:

```bash
gdi remote default drive
gdi status
gdi status drive --json
gdi push drive feature/login
```

`status` показывает текущую ветку, HEAD, изменения рабочего дерева и все подключения
с URL, Repository ID и состоянием публикации: не опубликована, опубликована,
локальная ветка впереди/позади, истории разошлись. Если remote commit ещё отсутствует
локально, статус предлагает `fetch` для сравнения; bundles сам не загружает.
Dirty worktree не меняет состояние публикации committed HEAD.
Когда текущий HEAD уже опубликован, состояние подключения — `Nothing to push.`.

На Drive имена каталогов читаемы: `branches/main/`, `branches/dev/`.
Для `feature/login` используется `branches/feature%2Flogin/`, а `%` кодируется `%25`.
Git exchange protocol v3 и local config v2 требуют пересоздания старых подключений;
инструкция перехода — [INSTALL.md](INSTALL.md).

## История публикаций

```bash
gdi log
gdi log -n 5
gdi log drive feature/login
gdi log --json
```

Подключение и ветка выбираются как у push/fetch. По умолчанию показываются последние
20 публикаций, от новой к старой. Каждая строка содержит сокращённый HEAD, вид bundle,
размер и сокращённый Publication ID; тема commit добавляется, если commit уже есть
локально. JSON содержит полные IDs, общее число публикаций и subject (null, если
commit отсутствует локально).

Это история публикаций на Drive: один push может включать несколько Git commits,
а полный checkpoint может повторно публиковать тот же HEAD. Для истории отдельных
commits используйте `git log`. Команда читает свежую identity и проверяет всю цепочку
metadata, даже с `-n 1`; bundles не скачивает, файлы и Git refs не изменяет.
На ещё не опубликованной ветке выводит `No publications for this branch`.

## Обычная работа агента

```text
поменять исходники → commit → push с CI → смотреть прогресс и лог
                                         ↓
                          FAIL → исправить → новый commit → новый CI
                                         ↓
                                        PASS
```

```bash
git add path/to/changed-file
git commit -m "Fix the requested behavior"
gdi push drive --ci --worker user-host --profile full --json
```

Ответ содержит `job_id`, `head`, `publication_id` и `profile_revision`.
Подставьте полученный `job_id` вместо `JOB_ID`:

```bash
gdi ci wait drive JOB_ID --follow --timeout 3600
gdi ci logs drive JOB_ID --output /tmp/ci-JOB_ID.log
```

При FAIL агент читает лог и отправляет исправление новым commit. Пользователю
не нужно вручную скачивать commits или пересылать консоль. Worker остаётся запущен
после FAIL и PASS. Несколько commits можно отправить одним push: проверяется HEAD.
В commit должны быть tracked YAML workflows; по умолчанию выбирается событие `push`.
Для конкретного workflow и другого события:

```bash
gdi push drive --ci --worker user-host --workflow .github/workflows/checks.yml --event workflow_dispatch --input MODE=full --json
```

`--job JOB_NAME` выбирает отдельный job workflow. Список CI команд в worker.json
не нужен. Лог в примере сохраняется вне репозитория, чтобы не мешать чистому pull.
`ci wait --follow` и `ci logs --follow` сохраняют позицию в `.gdi/ci/.../follow/`:
повтор команды продолжает после уже выведенных chunks. `--restart` вместе с
`--follow` воспроизводит лог с начала. Обычный `ci logs` и `logs --output` выдают
полный лог. При аварии между выводом chunk и сохранением позиции возможен повтор
последнего chunk; невидимый пользователю вывод не пропускается.

`ci wait` возвращает код 0 при проверенном PASS, 1 при другом terminal result,
124 при timeout ожидания.

## Получить проверенные изменения себе

В своём чистом Git-репозитории на той же ветке:

```bash
gdi pull drive --passed --job JOB_ID --profile full
```

Применяется именно commit выбранного PASS, даже если в Drive уже есть более новый.
Изменения должны быть fast-forward. Сохраните свои незакоммиченные файлы обычным Git
перед pull. gdi не делает автоматический stash и не сбрасывает вашу историю.

## Полезные команды

| Хочу | Команда |
| --- | --- |
| Посмотреть состояние задания | `gdi ci status drive JOB_ID` |
| Смотреть консоль по мере выполнения | `gdi ci logs drive JOB_ID --follow` |
| Воспроизвести консоль с начала | `gdi ci logs drive JOB_ID --follow --restart` |
| Снова ждать тот же job после Ctrl+C | `gdi ci wait drive JOB_ID --follow` |
| Отменить CI | `gdi ci cancel drive JOB_ID --json` |
| Явно повторить завершённый или отменяемый CI на том же commit | `gdi ci retry drive JOB_ID --json` |
| После проверенной отмены создать замену на другом host | `gdi ci retry drive JOB_ID --worker OTHER_WORKER_ID --json` |
| Отправить commits без CI | `gdi push drive` |
| Скачать историю, не менять файлы | `gdi fetch drive` |
| Получить последний commit без проверки CI | `gdi pull drive` |
| Посмотреть подключение и Repository ID | `gdi remote list` |
| Посмотреть службу | `gdi worker status` |
| Подробное состояние службы и последние сообщения | `systemctl --user status gdi-worker.service` |
| Последние 100 сообщений службы | `gdi worker logs` |
| Последние 200 сообщений за текущую загрузку | `gdi worker logs --boot -n 200` |
| Следить за логом службы | `gdi worker logs --follow` |
| Следить только за новыми сообщениями | `gdi worker logs -f -n 0` |
| Остановить службу | `gdi worker stop` |
| Запустить службу | `gdi worker start` |
| Перезапустить после изменения config/окружения/кода | `gdi worker restart` |
| Помощь / версия с эмблемой | `gdi -h` / `gdi -v` |

`worker logs` работает вне Git-репозитория; Ctrl+C останавливает только просмотр.
`worker restart` перечитывает systemd unit и перезапускает службу с актуальными config,
окружением и установленным кодом. При editable install подхватываются изменения исходников;
при обычной установке сначала обновите пакет.
При `worker stop` и остановке для `worker restart` worker перестаёт принимать новые
задания и ждёт завершения текущего CI и доставки результатов.
Unit использует `KillMode=mixed`: SIGTERM получает worker,
а act продолжает текущий запуск. По умолчанию systemd ждёт до **120 секунд**, затем
принудительно завершает группу процессов. Увеличьте лимит с учётом CI и передачи
файлов в Drive:

```bash
systemctl --user edit gdi-worker.service
```

Например, для 15 минут ожидания добавьте:

```ini
[Service]
TimeoutStopSec=900
```

Затем выполните `systemctl --user daemon-reload`. Это лимит остановки службы;
лимит самого CI задаётся `timeout_seconds` в worker.json.
Прерванное исполнение без сохранённого result станет INTERRUPTED; сохранённый
result будет доставлен без повторного исполнения. Ctrl+C или timeout у `ci wait`
не отменяют CI.

## Если что-то пошло не так

| Состояние | Что делать |
| --- | --- |
| QUEUED | Проверить `worker status` и journalctl; host должен быть включён и иметь доступ к Drive |
| FAIL | Читать полный лог; исправлять исходники и делать новый commit |
| ERROR | Читать `detail` и лог: инструменты, checkout, обязательные artifacts или окружение |
| TIMEOUT | CI превысил лимит host; проверить зависание и `timeout_seconds` в worker.json |
| INTERRUPTED | Worker перезапустился во время CI; изучить лог и явно выполнить `ci retry` |
| REJECTED | Проверить настройки и версию среды worker в journal; после исправления повторить `push --ci` |
| CANCEL_REQUESTED | Отмена на Drive; worker ещё не подтвердил остановку, offline host подтвердит после возвращения |
| CANCELLED | Остановка подтверждена проверенным результатом; при необходимости явно создать новую попытку |
| Ошибка сети | Повторить ту же команду; для готового результата worker повторяет загрузку без повторного CI |

Повтор `push --ci` для той же публикации при неизменённых настройках CI возвращает
прежний job ID при сохранённом локальном outbox. Новый запуск на том же commit —
через `ci retry`.

Отмена требует поддержки исходным worker; обновите gdi на host и клиентах и
перезапустите свободный worker перед использованием. `ci retry --worker ID`
допускается по проверенному terminal result либо доставленной поддерживаемой
отмене; отказ не обходят удалением файлов на Drive. Поздний результат отменённой
попытки блокируется, оригиналы сохраняются, подтверждение может находиться в
`ci/jobs/JOB_ID/cancelled/`. Для старого worker `ci cancel --withdraw` снимает
точное уведомление, но не доказывает остановку и не разрешает retry активного CI.
Автоматический перенос по таймауту включается отдельной политикой
[supervisor](docs/scheduling.md); автоматическая очистка spool пока не реализована.
Подробности — [docs/ci.md](docs/ci.md); команды агента — [docs/agent.md](docs/agent.md).

Не удаляйте ledger/spool worker для «разблокировки»: они нужны для восстановления.
При задержке результата смотрите journal службы: завершение CI и доставка/проверка
файлов на Drive — отдельные этапы. Замеры и статус реальных проверок: [TODO.md](TODO.md).

## Убрать старые bundles

```bash
gdi gc drive
```

Для применения сначала дождитесь доставки всех результатов, остановите worker и
приостановите обмен на всех машинах. Затем на одной машине:

```bash
gdi gc drive --apply --quiescent
gdi worker start
```

Незавершённые jobs и оставшиеся queue markers блокируют применение GC.
CI logs/results этим GC не удаляются. Подробнее: [docs/gc.md](docs/gc.md).

## Агент с Drive-коннектором

Исходники самого GDI можно получить через его GDI-репозиторий. Агент запускает
`python3 -m gdi agent ...` из исходников с Python и Git, без pip и rclone. GDI
готовит bundle/manifest и CI request/ready/inbox, агент передаёт их коннектором
и возвращает скачанные bytes для проверки. Команды, снимки, порядок загрузок
и восстановление проектов: [docs/agent.md](docs/agent.md).
