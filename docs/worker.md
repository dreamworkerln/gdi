# Настройка и эксплуатация worker

Worker — постоянный процесс на Linux host пользователя. Git/rclone и Python 3.10+
нужны для обмена; CI из GitHub Actions запускается через установленный `act` и Docker
Engine. Docker Compose требуется только если его использует сам workflow.
На каждом host задайте отдельный worker_id. Несколько workers могут читать общий
root, но выполняют только запросы, адресованные их ID. Два процесса/host с одним
ID не допускаются: локальный lock не защищает разные компьютеры. Клиент выбирает
исполнителя явно либо по свежему heartbeat/загрузке/меткам. Автоматический перенос
по таймауту использует отдельную политику [supervisor](scheduling.md).

## Общая конфигурация

Скопируйте [пример](../examples/worker.json) в `~/.config/gdi/worker.json`.
Задайте `INSTALL_DIR` — абсолютный путь к папке с исходниками gdi:

```bash
INSTALL_DIR="/путь/к/gdi"
mkdir -p "$HOME/.config/gdi"
cp "$INSTALL_DIR/examples/worker.json" "$HOME/.config/gdi/worker.json"
```

Укажите `remote_url`, например `gdrive:gdi`, и постоянный `worker_id`.
**Проекты, ветки и команды CI перечислять не нужно.** Проекты подключаются через
`gdi remote add`, а уведомления всех проектов поступают в общую `inbox`.
Протокол и локальные подключения: [inbox.md](inbox.md).

| Поле | Назначение |
| --- | --- |
| `config_version: 2` | Общая конфигурация worker |
| `worker_id` | ASCII ID worker, например `user-host` |
| `labels` | Необязательные публичные ASCII метки окружения/устройств; default `[]`, непустые входят в revision |
| `remote_url` | Общий корень обмена rclone, например `gdrive:gdi` |
| `timeout_seconds` | Общий лимит CI, по умолчанию 3600 |
| `poll_active_seconds` | Базовая пауза polling и live-публикаций CI, по умолчанию 30 секунд |
| `poll_idle_max_seconds` | Максимальная пауза в простое/при сетевых ошибках, по умолчанию 120 секунд |
| `state_dir`, `cache_dir` | Необязательные абсолютные пути вместо XDG defaults |
| `act_executable` | `act` в PATH службы или абсолютный путь к программе |
| `act_version` | Требуемая версия act, default `0.2.89`; несовпадение блокирует capabilities |
| `docker_executable` | Docker CLI в PATH или абсолютный путь, default `docker` |
| `platforms` | Сопоставление `runs-on` и образов; default: `ubuntu-latest=catthehacker/ubuntu:act-latest` |
| `artifact_server_port` | Порт локального сервера артефактов act, по умолчанию 34567 |
| `artifact_server_addr` | IPv4 адрес сервера artifacts/cache, default `127.0.0.1` для host network/self-hosted |
| `secret_names` | Имена secrets, которые act возьмёт из окружения службы; default `[]` |
| `transport.connect_timeout_seconds` | rclone connection timeout, по умолчанию 10 |
| `transport.timeout_seconds` | rclone I/O idle timeout, по умолчанию 60 |
| `transport.retries`, `low_level_retries` | Число сетевых попыток, по умолчанию 3 |

Rclone credentials остаются в его конфигурации; gdi их не копирует. Значения secrets
не записывайте в worker.json. Передавайте их через systemd EnvironmentFile и
перечисляйте только имена в `secret_names`. Сам worker config на Drive не отправляется.

На Linux worker использует временный native RC: один процесс для advertise,
отдельный для discovery и отдельный для обработки каждого задания. Процесс
закрывается в конце операции, включая ошибку; во время обработки задания его
использует также publisher логов. FS cache сбрасывается при проверке repository ID,
поэтому пересоздание папки не сохраняет старую привязку. Linux завершает RC и при
SIGKILL worker. `transport` задаёт сетевые timeout/retries для RC; для диагностики
можно задать `GDI_TRANSPORT=cli` в окружении службы и перезапустить её.
Учёт процессов и RC-обращений описан в [profiling.md](profiling.md).

Для обычного CI рекомендуются `poll_active_seconds: 30` и
`poll_idle_max_seconds: 120`. Это выбор gdi, а не обязательная минимальная пауза
Google: [Drive API ограничивает суммарную нагрузку квотами](https://developers.google.com/workspace/drive/api/guides/limits),
которые зависят от Cloud project. При quota/rate-limit ошибках нужны задержки
повторов; rclone управляет своими сетевыми попытками, worker увеличивает паузу
между циклами до настроенного максимума. Один цикл gdi может означать несколько
HTTP-запросов: listing inbox, разрешение путей и чтение уведомлений.

При непрерывном опросе раз в 5 секунд получается до 17 280 циклов в сутки,
раз в 30 секунд — до 2 880, раз в 60 секунд — до 1 440,
раз в 120 секунд — до 720; сетевые операции
уменьшают эти числа. Несколько включённых workers суммируют нагрузку.
Сам по себе интервал 5 секунд не означает нарушение квоты, но для ожидания
CI создаёт лишние обращения. Ответы `403 User rate limit exceeded` и
`429 Rate limit exceeded` указывают на ограничение API; `TLS handshake timeout`
и `connection refused` относятся к сети/прокси и сами по себе не означают квоту.

Локальный heartbeat выполнения обновляется раз в 5 секунд и не вызывает запросов
к Drive. Периодическая публикация статуса и новых log chunks использует базовую
паузу `poll_active_seconds`; доставка итогового результата начинается сразу
после завершения CI. Паузы считаются после сетевой операции, поэтому фактический
интервал больше на длительность этой операции. Для уменьшения постоянных listings
в дальнейшем предусмотрены [Drive change notifications](https://developers.google.com/workspace/drive/api/guides/push).

В простое worker также обновляет status на Drive после discovery. Registry heartbeat
содержит `registry_version: 1`, READY/BUSY, `busy`, известную локальную очередь,
revisions profiles, метки и platforms. При выполнении эти сведения публикуются
вместе с захваченным job heartbeat; старое updated_at не обновляется просто из-за
повторной отправки готового результата. Доступность и ограничения очереди:
[scheduling.md](scheduling.md).

Совместимость act `0.2.89` с новыми artifact actions и воспроизводимая
сборка с upstream-патчем описаны в [act-compatibility.md](act-compatibility.md).

## CI берётся из проверяемого commit

По умолчанию выполняются workflows каталога `.github/workflows` для события `push`.
Команды, `uses`, matrix, зависимости jobs и `continue-on-error` исполняет act.
Выбрать другой файл, событие, job и inputs можно при отправке:

```bash
gdi push drive --ci --worker user-host --json
gdi push drive --ci --worker user-host --workflow .github/workflows/checks.yml --event workflow_dispatch --input MODE=full --json
```

`full` сохраняется как имя общего исполнения для совместимости CLI; отдельного
профиля проекта в worker.json нет. Request v2 закрепляет selector и SHA, а revision —
общие настройки исполнения host. После изменения config перезапустите службу.
Старый запрос с другой revision получает REJECTED и требует явного повторного submit.

Act получает отдельный checkout точного commit, `GITHUB_REF` и SHA. Обычный
`actions/checkout` использует переданные локальные исходники. Workspaces jobs копируются;
исходный checkout и пользовательская рабочая ветка не используются для build outputs.
Изолированный checkout содержит локальный ref запрошенной ветки на точный SHA,
при этом HEAD остаётся detached. В контейнер копируются также его git metadata,
поэтому `git rev-parse HEAD` и чтение истории работают внутри workflow.
Act запускается вне checkout: project `.actrc` не заменяет выбранный план. Docker
images и actions используют persistent caches. При advertise worker проверяет
`act_version`, SHA256 бинарника, host `.actrc`, Docker daemon ID/version и фактические
image IDs из `platforms`. Эти значения входят в execution revision. Все выбранные
base images должны уже присутствовать локально; загрузите их через `docker pull`
перед запуском. Act получает immutable image IDs и `--pull=false`; обновление
локального image tag меняет revision после restart. Обновление act с прежним номером
версии также выявляется по SHA256. Отчёт включён в проверяемый `artifacts/environment.json`.

Имя `owner/repository` берётся из GitHub origin отправителя и закрепляется в request
v2 без URL/credentials. Worker передаёт repository/owner в event и act contexts;
retry сохраняет прежний namespace. При отсутствии GitHub origin эти поля не
подменяются придуманным namespace. Pull request API payload и остальные GitHub
contexts автоматически не воспроизводятся.

Docker CLI и act используют один явный daemon endpoint. Jobs без `services` работают
в host network. Если workflow создаёт bridge network для services, задайте
`artifact_server_addr` как IPv4 host, достижимый из контейнеров. Значение `127.0.0.1`
для такой сети не подходит. Внешние actions и собственные `container`/`services`
images задаёт YAML; их версии/digests закрепляйте в проекте отдельно.

Консоль act сохраняется как JSON lines в общем build.log и передаётся streaming chunks.
В результате gdi это одна blocking-стадия `github-actions`; детализация jobs/steps
сохраняется в консоли. Ненулевой exit act даёт FAIL; exit 0 без успешного завершённого
job даёт ERROR, а не PASS. Upload artifacts собираются сервером act и публикуются
как `artifacts/workflow-artifacts.zip`, содержащий его artifact store.

[Act не полностью совместим с GitHub Actions](https://nektosact.com/not_supported.html):
в частности, не все GitHub contexts, permissions, cancellation и job timeouts
воспроизводятся. Общий timeout обеспечивает gdi: сначала SIGINT для cleanup act,
через 10 секунд при необходимости SIGKILL. Перед исполнением worker сохраняет
Docker daemon identity и уникальный checkout в durable ownership record. После CI
и при restart он удаляет только containers `act-*` с WorkingDir данного checkout;
чужие containers сохраняются. Ошибка cleanup оставляет job в recovery до восстановления
исходного daemon. Docker volumes/networks и containers, созданные shell-командами
самого workflow, требуют отдельного обслуживания; глобальный prune не выполняется.
Это проверяется на Docker отдельно от host/self-hosted fixture.
Windows/macOS jobs требуют соответствующей среды; Linux Docker не заменяет эти ОС.

Установку [Docker Engine](https://docs.docker.com/engine/install/) и
[act](https://nektosact.com/installation/) выполняет владелец host. Проверочная версия
act — v0.2.89. Убедитесь, что пользователь службы имеет доступ к Docker Engine.

## Проверка и первый запуск

```bash
gdi worker check --config ~/.config/gdi/worker.json --json
gdi worker check --config ~/.config/gdi/worker.json --runtime --json
act --version
docker version
rclone lsf gdrive:gdi
gdi worker run --config ~/.config/gdi/worker.json
```

`check` проверяет схему config и выдаёт `config_revision`; `check --runtime`
проверяет act/Docker/images и выдаёт фактические `execution_revision` и environment.
Поле `worker_id` в выводе `gdi worker check --json` — ID этого host из
`~/.config/gdi/worker.json`; при другом пути укажите `--config PATH`.
Для CI передавайте это значение как `--worker WORKER_ID`. Проверка config
не подтверждает, что служба работает; её состояние показывает `gdi worker status`.
Доступ к Drive проверяется при запуске. Foreground worker создаёт capabilities и inbox,
остаётся работать после PASS/FAIL. Ctrl+C запрашивает завершение после текущего job.
Для одного обхода: `gdi worker run --config ~/.config/gdi/worker.json --once`.
Сетевые ретраи не повторяют уже исполненный CI.

Legacy config version 1 со списком repositories/stages пока читается для обновления
старых установок. Он использует прежние per-repository queues и команды host.
Для новой настройки используйте version 2. Legacy queue не опрашивается новым global
worker; сначала завершите старые задания, затем переходите на общий root.

## Systemd user service

После проверки foreground остановите его и выполните:

```bash
gdi worker install --config ~/.config/gdi/worker.json
gdi worker start
gdi worker status
```

Install записывает `gdi-worker.service` в `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`,
делает daemon-reload. Start выполняет `enable --now`; после этого служба запускается
при запуске user manager. Unit сохраняет абсолютные пути к текущему Python
(включая venv) и config, не использует cwd или `.bashrc`. Пользовательская копия
rclone должна быть в PATH службы; при необходимости задайте PATH в override.

При необходимости отредактируйте окружение через:

```bash
systemctl --user edit gdi-worker.service
```

Например, `[Service]` и `EnvironmentFile=/абсолютный/путь/worker.env` для локальных
секретов/настроек. Содержимое файла не отправляется в capabilities. В нём можно
задать `RCLONE_CONFIG=/абсолютный/путь/rclone.conf`, если используется нестандартный
config. Затем выполните `gdi worker restart`: команда делает daemon-reload и перезапускает
службу. Новый процесс перечитывает worker.json, окружение и установленный Python-код.
При editable install достаточно изменить исходники и вызвать `gdi worker restart`;
при обычной установке сначала обновите пакет. Если изменился путь к Python или config,
повторите `gdi worker install --config ...` перед restart.

Если сеть требует прокси, задайте `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` и/или
их варианты в нижнем регистре в этом EnvironmentFile. Worker передаёт эти
переменные как rclone, так и контейнерам act; остальные переменные окружения
host автоматически в контейнер не передаются. Адреса прокси не записываются
в аргументы запуска act или capabilities: они передаются через локальный файл
окружения с правами 0600 вне checkout и публикуемых artifacts.
При включённом прокси worker сохраняет
исключения `NO_PROXY`/`no_proxy` и добавляет localhost, loopback и адрес локального
artifact/cache server, чтобы загрузка артефактов не уходила через прокси.

Для работы без открытой login-сессии владелец host может включить linger:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Это отдельная настройка операционной системы. gdi не включает linger автоматически.

## Два разных лога

```bash
gdi worker logs
gdi worker logs --boot -n 200
gdi worker logs --follow
gdi worker logs -f -n 0
```

По умолчанию показываются последние 100 сообщений. `-n`/`--lines` меняет их число,
`-b`/`--boot` выбирает текущую загрузку системы. `-f`/`--follow` показывает новые
сообщения по мере поступления; `-n 0` пропускает старые записи. Ctrl+C завершает
просмотр и дочерний journalctl, worker продолжает работать. Команда работает
вне Git-репозитория, не требует worker config и использует user journal
`journalctl --user -u gdi-worker.service --no-pager`.

В journal: запуск службы, job ID, этапы, диагностика сети и публикации.
Полная stdout+stderr консоль CI сохраняется отдельно в job `build.log`, а также
публикуется chunks во время выполнения. Агент получает её командами `gdi ci logs`
и `gdi ci wait --follow`. Сохранность journal между reboot определяется настройками
journald, а не gdi. Локальный spool не зависит от journal.

## Состояние, кеш и восстановление

По умолчанию:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/gdi/<worker-id>/
    ledger.sqlite3
    jobs/<job-id>/build.log, events/, log-chunks/, result.json, artifacts/, checkout/
${XDG_CACHE_HOME:-$HOME/.cache}/gdi/<worker-id>/<repository-id>/receiver/
    .gdi/cache/<repository-id>/repository.git
```

Ledger и spool постоянные; не удаляйте их при активных/недоставленных jobs.
Историю можно восстановить без кеша из последнего full bundle и следующих дельт.
Для старого запроса worker восстанавливает текущую историю ветки, проверяет исходную
публикацию и reachability выбранного SHA, затем делает отдельный detached checkout.
Это работает и после GC старой упаковки выбранной публикации.

| Обнаружено после restart | Поведение |
| --- | --- |
| Подготовка не дошла до RUNNING | Подготовить checkout снова, сохранив job/run IDs |
| RUNNING/FINALIZING без durable result | Не запускать снова; остановить известный собственный процесс с совпавшей Linux identity, сохранить INTERRUPTED |
| Локальный result уже записан | Проверить отмену и spool; повторить только upload либо доставить CANCELLED в отдельном cancelled/ |
| Result опубликован, уведомление inbox или legacy queue pointer остался | Перепроверить result и повторить удаление конкретного marker, без CI |
| Claim принадлежит другому run или ledger потерян | Явная ошибка в journal, автоматического takeover нет |

Live publisher работает отдельно от чтения stdout. Недоступный Drive не блокирует
захват консоли; закрытые chunks, log и result остаются на диске. Результат публикуется
последним после artifacts. Сбой upload переводит доставку в UPLOAD_PENDING;
последующие обходы повторяют её с теми же bytes, не выполняя команды снова.
Heartbeat/status загружается из отдельного неизменяемого снимка, чтобы обновление
локального status.json во время передачи не вызывало checksum mismatch.
Статусы job и worker за один проход используют одни и те же bytes снимка.

Worker объявляет `cancel_version:1`. Отмена сохраняется в `cancel.json` локального
spool и проверяется до запуска, во время исполнения и перед/после публикации.
При поздней отмене исходные immutable файлы остаются историей, подтверждённый
CANCELLED доставляется отдельным комплектом в `jobs/<job-id>/cancelled/` локально
и `ci/jobs/<job-id>/cancelled/` на Drive. Restart продолжает эту доставку без
повторного исполнения. Подробнее — [ci-protocol.md](ci-protocol.md).

При остановке `gdi worker stop` или перезапуске `gdi worker restart` worker не берёт
новые jobs и заканчивает текущий.
Unit использует `KillMode=mixed`: SIGTERM получает только worker, чтобы act и
его дочерние процессы могли закончить текущий CI. Systemd ждёт до 120 секунд,
затем убивает всю control group. На следующем старте
незавершённый запуск становится INTERRUPTED, а готовый result допубликовывается.
Если CI вместе с доставкой результатов длится дольше, увеличьте `TimeoutStopSec`
через override.
Условия и команды реальной проверки: [acceptance.md](acceptance.md).
При SIGKILL foreground вне systemd могут остаться потомки: контролируйте их на host;
никогда не начинайте повторный запуск вслепую. Exactly-once не обещается.

Job можно явно повторить через `gdi ci retry` после проверки terminal result
либо доставки поддерживаемой отмены; `--worker ID` выбирает нового исполнителя.
Обычные wait/logs не отменяют job по таймауту. Автоматическая отмена и замена
выполняются только запущенным `ci supervise`/`agent ci supervise` с заданной политикой.
Бесконечный рост локального spool и CI artifacts пока ограничивается обслуживанием
оператором после получения результатов; автоматической retention policy нет.
Удаление ledger при сохранённых remote claims требует ручной диагностики и не
является поддерживаемым способом «сбросить очередь».
