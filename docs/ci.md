# CI через GDI и rclone

Актуально для gdi 0.3.0: Git protocol v3, CI protocol v1/v2. Пользователь или агент
с настроенным rclone публикует точный commit, постоянный worker пользователя запускает
выбранные `.github/workflows` через act, клиент получает проверенный результат
и весь консольный вывод.

Настройка host: [INSTALL.md](../INSTALL.md), [worker.md](worker.md).
Человеческий справочник: [QUICKSTART.md](../QUICKSTART.md).

## Где выполняются агент и CI

Агент может работать в облачном контейнере провайдера, а постоянный worker —
на компьютере пользователя. Google Drive связывает эти две среды: агент
передаёт Git-публикации и CI requests, worker локально выполняет act/Docker
и публикует консоль, artifacts и result. Рабочий clone пользователя не меняется.

Далее описаны команды обычного CLI с rclone. Для агента с Drive-коннектором
основная инструкция — [agent.md](agent.md): запуск CLI из исходников GDI,
получение проекта, возврат изменений и подготовка CI без rclone и установки пакета.

## Перед первой отправкой

Прочитайте действующие инструкции репозитория. Убедитесь, что задача разрешает
создание commits и отправку на CI. Уже полученное разрешение сохраняется на цикл
исправлений; не просите пользователя повторять его на каждой итерации.

Должны быть известны рабочая ветка, gdi remote, Repository ID, ID worker и название
полного профиля. Например: `dev`, `drive`, известный ID, `user-host`, `full`.
Обычный Git remote и gdi remote — разные настройки; `gdi remote list` показывает
именно подключения через Drive.

```bash
gdi -v
gdi remote list
git status --short
git branch --show-current
```

Первое подключение к уже созданной папке:

```bash
gdi remote add drive gdrive:gdi/my-project --repository-id REPOSITORY_ID
```

Не используйте `--init` на существующей папке. Не устанавливайте службу и не меняйте
credentials пользователя в рамках задачи с исходниками. Host должен уже быть
настроен его владельцем. Не запускайте второй worker для того же remote.

## Исходники → commit → job

Изучите задачу, измените исходники, выполните доступные локальные проверки.
Коммитьте только относящиеся к задаче файлы. Проверьте отсутствие credentials и
лишних build outputs в commit.

```bash
git add path/to/changed-file
git commit -m "Fix requested behavior"
git rev-parse HEAD
gdi push drive --ci --worker user-host --profile full --json
```

gdi сам commit не создаёт. Незакоммиченные изменения не отправляются. Несколько commits
можно отправить одним push, CI проверит выбранный HEAD. Не изменяйте ту же ветку
параллельно с push. Ответ JSON содержит `job_id`, `head`, `ref`, `publication_id`,
`worker_id`, `profile_id`, `profile_revision` и `repository_id`; сохраните их.

Request использует возвращённую Git-публикацию. Изменение локального HEAD после
отправки не меняет задание. Профиль и revision берутся из capabilities worker.
Если worker не объявил общие execution capabilities, submit завершится ошибкой, хотя Git push
может уже успешно завершиться. После настройки повторите ту же команду.

Для ранее опубликованной версии:

```bash
gdi ci submit drive --publication PUBLICATION_ID --worker user-host --profile full --json
```

Outbox сохраняется в `.gdi/ci/<repository-id>/outbox/` основного worktree до upload.
Повтор после сетевой ошибки допубликовывает те же bytes с тем же job ID. Повтор
успешного submit возвращает прежний job, даже если он уже завершён. Не удаляйте outbox
ради повторного исполнения; используйте `ci retry`. После потери outbox сначала
проверьте ранее сохранённый ID, глобальная дедупликация не обещается.

## Следить за исполнением и получить консоль

```bash
gdi ci status drive JOB_ID --json
gdi ci wait drive JOB_ID --follow --timeout 3600 --json
```

`wait --json` выдаёт один итоговый JSON в stdout; прогресс, stage, heartbeat и
консоль идут в stderr. Для автоматизации сохраняйте потоки отдельно. `verified: true`
означает, что terminal result, идентичность request и контрольные суммы всех artifacts
проверены, полный log совпал с непрерывной последовательностью chunks.

Полный лог можно сохранить после завершения, включая FAIL:

```bash
gdi ci logs drive JOB_ID --output /tmp/gdi-JOB_ID.log
```

Без `--output` команда показывает уже опубликованные фрагменты; с `--follow` ждёт
окончания и показывает новые. Бинарные bytes сохраняются без преобразований.
`wait --follow` и `logs --follow` сохраняют позицию между вызовами. Повтор команды
продолжает после уже выведенных chunks; `--restart` вместе с `--follow` показывает
лог с начала. Обычный `logs` и `logs --output` читают полный лог независимо от позиции.
В локальной `.gdi` основного worktree также остаются проверенные result и artifacts:
`ci/<repository-id>/results/<job-id>/`.

| Exit `ci wait` | Значение |
| --- | --- |
| 0 | Проверенный PASS |
| 1 | Проверенный FAIL/ERROR/TIMEOUT/INTERRUPTED/REJECTED либо ошибка gdi; различайте JSON `state` и `kind: GDI_ERROR` |
| 2 | Ошибка аргументов |
| 124 | Истёк timeout ожидания клиента; job продолжает работать |
| 130 | Прервано ожидание, job не отменён |

Status и logs возвращают 0 при успешном чтении независимо от исхода CI.
При сетевой ошибке ожидание можно продолжить тем же ID. Advisory `status.json`
не доказывает PASS и может отставать; терминальным доказательством служит проверенный result.

## Итерации до PASS

| Итог | Действие агента |
| --- | --- |
| FAIL | Читать `failed_stage`, `exit_code` и полный лог; исправить исходники, создать новый commit, новый `push --ci` |
| ERROR | Разобрать `detail`, лог и ошибки подготовки/инструментов/artifacts; менять host config только в разрешённом объёме |
| TIMEOUT | Проверить зависание; host ограничивает время исполнения отдельно от времени ожидания агента |
| INTERRUPTED | Исполнение после сбоя неопределённо; изучить лог и явно запросить новую попытку |
| REJECTED | Профиль неизвестен/изменился; после настройки отправить новую заявку с текущей revision |
| PASS | Проверить точные head/profile/revision, сообщить итог и способ получить проверенный commit |

```bash
# Только для завершённой попытки на прежнем commit:
gdi ci retry drive JOB_ID --json
```

Retry создаёт новый ID с `retry_of`, сохраняя выбранную публикацию и используя текущую
revision профиля. Повтор самой команды retry переиспользует созданную попытку через
outbox. Для ещё активного job retry запрещён. Upload готового результата worker
повторяет самостоятельно; отправлять новый job из-за временной ошибки upload не нужно.
Worker после PASS и FAIL продолжает принимать задания. STOP-файлов нет.

## Передать результат пользователю

Сообщите commit SHA, job ID, профиль/revision, исход CI и существенные детали проверки.
У пользователя исходный worktree не менялся. Чтобы получить именно проверенный commit:

```bash
gdi pull drive --passed --job JOB_ID --profile full
```

Команда проверяет выбранный PASS и применяет его SHA через fast-forward на текущей
совпадающей ветке. Более новый непроверенный tip не подставляется. Dirty/divergent
дерево требует обычного разрешения средствами Git; gdi его не сбрасывает.

Для чтения истории без изменения файлов есть `gdi fetch drive`. Обычный `gdi pull drive`
выбирает последний опубликованный tip без CI gate; не используйте его как эквивалент
`pull --passed`.

## Обслуживание

GC bundles выполняется отдельно между задачами. Все результаты должны быть доставлены,
очередь пуста, worker и остальные клиенты приостановлены. `gdi gc drive` — только план;
`--apply --quiescent` проверяет сохранённую историю и отказывает при незавершённых CI
jobs. Старый gdi 0.2.1 не знает CI-защиту GC: обновите все клиенты до 0.3 перед применением.
Сами CI logs/results и локальный spool автоматически не очищаются.

В config version 2 worker опрашивает общую inbox всех проектов. Список репозиториев
и команды CI в host config не требуются. Для выбора проверки используйте
`--workflow PATH`, `--event EVENT`, `--job ID`, `--input KEY=VALUE`; selector закрепляется
в request и сохраняется при retry. Не добавляйте shell-команды в другие configs.
Подключения хранятся в `.gdi/config.json`, Git config не изменяется. `.gdi/` должна
оставаться локальной и не попадать в commits. Протокол: [inbox.md](inbox.md).

Execution revision связывает настройки с фактическим act/Docker/base image окружением.
После обновления host tools/images требуется restart worker; новые capabilities
могут потребовать resubmit. Проверяемый отчёт — `artifacts/environment.json`.
Namespace GitHub из обычного origin закрепляется в request v2 и сохраняется при retry.
Перед push CLI проверяет синтаксис selector и наличие обычных tracked YAML файлов
в выбранном commit для global worker. YAML/job/event semantics проверяет act.
