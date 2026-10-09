# gdi: краткий справочник для человека

gdi переносит commits через Google Drive и запускает CI на вашем компьютере.
Агент получает прогресс, консоль и результат, исправляет ошибку и отправляет новый
commit. Ваш рабочий каталог при этом не меняется.

Полная первоначальная настройка Python, rclone, Google OAuth и worker:
[Install.md](Install.md). Здесь предполагается, что она уже выполнена.
`drive` — подключение gdi, `gdrive:` — подключение rclone, `user-host` — ID worker,
`full` — общее имя исполнения workflows; команды берутся из `.github/workflows` commit.

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
На host один раз задайте общий `remote_url: "gdrive:gdi"` в worker config version 2,
установите act и Docker. Новые проекты и ветки не требуют изменения worker.json.
Затем:

```bash
gdi worker check --config ~/.config/gdi/worker.json
gdi worker install --config ~/.config/gdi/worker.json
gdi worker start
```

Установка службы не запускает её до `worker start`. Для одного remote работает
один worker на общий root. Агент выбирает workflows из commit и не передаёт argv
в запросе. Общая inbox всех проектов опрашивается без обхода папок репозиториев.

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
gdi ci logs drive JOB_ID --output ./ci-JOB_ID.log
```

При FAIL агент читает лог и отправляет исправление новым commit. Пользователю
не нужно вручную скачивать commits или пересылать консоль. Worker остаётся запущен
после FAIL и PASS. Несколько commits можно отправить одним push: проверяется HEAD.

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
| Снова ждать тот же job после Ctrl+C | `gdi ci wait drive JOB_ID --follow` |
| Явно повторить завершённый CI на том же commit | `gdi ci retry drive JOB_ID --json` |
| Отправить commits без CI | `gdi push drive` |
| Скачать историю, не менять файлы | `gdi fetch drive` |
| Получить последний commit без проверки CI | `gdi pull drive` |
| Посмотреть подключение и Repository ID | `gdi remote list` |
| Посмотреть службу | `gdi worker status` |
| Лог службы за текущую загрузку | `journalctl --user -u gdi-worker.service -b` |
| Следить за логом службы | `journalctl --user -u gdi-worker.service -f` |
| Остановить службу | `gdi worker stop` |
| Запустить службу | `gdi worker start` |
| Помощь / версия с эмблемой | `gdi -h` / `gdi -v` |

`worker stop` перестаёт принимать новые задания и даёт текущему закончиться.
Systemd ждёт до 120 секунд, затем завершает группу процессов; при следующем запуске
неопределённый запуск станет INTERRUPTED. Ctrl+C или timeout у `ci wait` не отменяют CI.

## Если что-то пошло не так

| Состояние | Что делать |
| --- | --- |
| QUEUED | Проверить `worker status` и journalctl; host должен быть включён и иметь доступ к Drive |
| FAIL | Читать полный лог; исправлять исходники и делать новый commit |
| ERROR | Читать `detail` и лог: инструменты, checkout, обязательные artifacts или окружение |
| TIMEOUT | CI превысил лимит host; проверить зависание и настройки профиля |
| INTERRUPTED | Worker перезапустился во время CI; изучить лог и явно выполнить `ci retry` |
| REJECTED | Профиль отсутствует или изменился; после настройки повторить `push --ci` |
| Ошибка сети | Повторить ту же команду; для готового результата worker повторяет загрузку без повторного CI |

Повтор `push --ci` для той же публикации/профиля возвращает прежний job ID при
сохранённом локальном outbox. Новый запуск на том же commit — через `ci retry`.
Не удаляйте ledger/spool worker для «разблокировки»: они нужны для восстановления.

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
