# Настройка и эксплуатация worker

Worker — постоянный процесс на Linux host пользователя. Работает на Python 3.10.12,
использует Git, rclone и стандартную библиотеку Python. Один процесс выполняет
задания последовательно для одного или нескольких зарегистрированных проектов.
На каждый Drive remote назначайте ровно один worker.

## Конфигурация

Скопируйте [пример](../examples/worker.json) в `~/.config/gdi/worker.json`.
Замените `repository_id` на ID из `gdi remote list`, `remote_url` на ваш rclone URL,
а `stages` — на настоящий полный CI проекта. Пример запускает тесты самого gdi.
Для проекта с собственным CI-скриптом стадия может выглядеть так:

```json
{
  "name": "full-ci",
  "argv": ["/usr/bin/bash", "ci/run.sh"],
  "cwd": ".",
  "blocking": true,
  "timeout_seconds": 3600
}
```

`argv` исполняется без shell. Bash используется только если вы явно указали его
как программу. `~`, `$HOME`, `&&`, перенаправления и glob в аргументах сами не
раскрываются. `cwd` и artifact paths относительны к изолированному checkout.
Абсолютные executable paths удобны для systemd; можно указать Python вашего
проектного venv, например `/home/$USER/.venvs/project/bin/python`, **заменив `$USER`
реальным именем в JSON**. Shell-подстановок в JSON нет.

| Поле | Назначение |
| --- | --- |
| `config_version: 1` | Версия локальной конфигурации |
| `worker_id` | ASCII ID worker, например `user-host` |
| `repositories` | Список зарегистрированных repository IDs/URLs и profiles |
| `profiles.full.timeout_seconds` | Общий лимит исполнения стадий, по умолчанию 3600 |
| `stages[].name`, `argv` | Уникальное имя стадии и список аргументов |
| `stages[].cwd` | Каталог внутри checkout, по умолчанию `.` |
| `stages[].blocking` | Обязательная стадия, по умолчанию true |
| `stages[].timeout_seconds` | Лимит стадии, по умолчанию общий лимит |
| `profiles.full.env` | Не секретные переменные окружения; по умолчанию `{}` |
| `profiles.full.artifacts` | Список разрешённых выходных файлов; по умолчанию `[]` |
| `poll_active_seconds` | Пауза после активности, по умолчанию 5 |
| `poll_idle_max_seconds` | Максимальная пауза в простое/при сетевых ошибках, по умолчанию 60 |
| `state_dir`, `cache_dir` | Необязательные абсолютные пути вместо XDG defaults |

Профиль обязан содержать хотя бы одну blocking-стадию. Ненулевой exit blocking-стадии
даёт FAIL и прекращает план. Ошибка non-blocking стадии записывается в warnings.
Таймаут останавливает группу процессов и даёт TIMEOUT. Невыполненная обязательная
стадия не даёт PASS. Worker проверяет HEAD и отсутствие изменений tracked sources
после CI; untracked build outputs допустимы.

Revision рассчитывается автоматически по нормализованным настройкам, включая команды,
timeouts, env и artifacts. Поле `revision` в config добавлять не нужно. Сам config
на Drive не отправляется. Secrets держите в окружении host/EnvironmentFile systemd,
а не в `env`: изменение унаследованных secrets не меняет revision. Не печатайте
secrets в консоль — CI logs публикуются в папку remote.

Пример дополнительного artifact внутри профиля:

```json
"artifacts": [
  {"name": "firmware.bin", "path": "build/firmware.bin", "required": true}
]
```

Публикуются только явно перечисленные files. `..`, выход за checkout и symlink наружу
запрещены. Отсутствующий required artifact даёт ERROR. CI исполняется с правами
пользователя worker; checkout не является контейнером или OS sandbox.

## Проверка и первый запуск

```bash
gdi worker check --config ~/.config/gdi/worker.json --json
rclone lsf gdrive:gdi/my-project
gdi worker run --config ~/.config/gdi/worker.json
```

`check` проверяет локальную схему и показывает revisions; доступность Drive и
проектных инструментов проверяются при run. В foreground worker создаёт capabilities,
показывает служебные сообщения в консоли и остаётся работать после завершения jobs.
Ctrl+C запрашивает завершение после текущего job. Для отладки одного обхода очереди:

```bash
gdi worker run --config ~/.config/gdi/worker.json --once
```

Перед запуском установите зависимости **самого проверяемого проекта** в окружении
host: компилятор, SDK, Python venv и т. п. gdi их автоматически не устанавливает.
Команда должна выполнять полный CI, согласованный с агентом, а не только быструю
подпроверку. Изолированный checkout не содержит ваших игнорируемых локальных файлов.

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
config. Затем `systemctl --user daemon-reload` и `systemctl --user restart gdi-worker.service`.
При изменении worker.json также перезапустите службу: профили читаются при старте.

Для работы без открытой login-сессии владелец host может включить linger:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Это отдельная настройка операционной системы. gdi не включает linger автоматически.

## Два разных лога

```bash
journalctl --user -u gdi-worker.service -b
journalctl --user -u gdi-worker.service -f
```

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
    .git/gdi-cache/<repository-id>/repository.git
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
| Локальный result уже записан | Проверить spool и повторить только upload |
| Result опубликован, queue pointer остался | Завершить подтверждение/очистку очереди, без CI |
| Claim принадлежит другому run или ledger потерян | Явная ошибка в journal, автоматического takeover нет |

Live publisher работает отдельно от чтения stdout. Недоступный Drive не блокирует
захват консоли; закрытые chunks, log и result остаются на диске. Результат публикуется
последним после artifacts. Сбой upload переводит доставку в UPLOAD_PENDING;
последующие обходы повторяют её с теми же bytes, не выполняя команды снова.

При остановке `gdi worker stop` worker не берёт новые jobs и заканчивает текущий.
Systemd ждёт до 120 секунд, затем убивает всю control group. На следующем старте
незавершённый запуск становится INTERRUPTED, а готовый result допубликовывается.
Если полный CI длится дольше, увеличьте `TimeoutStopSec` через override.
При SIGKILL foreground вне systemd могут остаться потомки: контролируйте их на host;
никогда не начинайте повторный запуск вслепую. Exactly-once не обещается.

Уже опубликованный terminal job можно явно повторить через `gdi ci retry`.
Бесконечный рост локального spool и CI artifacts пока ограничивается обслуживанием
оператором после получения результатов; автоматической retention policy нет.
Удаление ledger при сохранённых remote claims требует ручной диагностики и не
является поддерживаемым способом «сбросить очередь».
