# Инструкция агенту: автономный CI через gdi

Актуально для gdi 0.3.0: Git protocol v2, CI protocol v1/v2. Агент публикует точный
commit, постоянный worker пользователя запускает выбранные `.github/workflows` через act,
агент получает проверенный результат и весь консольный вывод. Ручная передача логов
между пользователем и агентом не требуется.

Настройка host: [INSTALL.md](INSTALL.md), [docs/worker.md](docs/worker.md).
Человеческий справочник: [QUICKSTART.md](QUICKSTART.md).

## Где выполняются агент и CI

Агент может работать в облачном контейнере провайдера, а постоянный worker —
на компьютере пользователя. Google Drive связывает эти две среды: агент
передаёт Git-публикации и CI requests, worker локально выполняет act/Docker
и публикует консоль, artifacts и result. Рабочий clone пользователя не меняется.

Далее описаны команды установленного CLI. Если в контейнере агента GDI нет
и установить его нельзя, используйте раздел [«Облачный агент без установленного GDI»](#облачный-агент-без-установленного-gdi):
он описывает получение commit, отправку уже опубликованной версии на CI
и проверку результата через инструменты Drive, Python и Git без CLI.

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
оставаться локальной и не попадать в commits. Протокол: [docs/inbox.md](docs/inbox.md).

Execution revision связывает настройки с фактическим act/Docker/base image окружением.
После обновления host tools/images требуется restart worker; новые capabilities
могут потребовать resubmit. Проверяемый отчёт — `artifacts/environment.json`.
Namespace GitHub из обычного origin закрепляется в request v2 и сохраняется при retry.
Перед push CLI проверяет синтаксис selector и наличие обычных tracked YAML файлов
в выбранном commit для global worker. YAML/job/event semantics проверяет act.

## Облачный агент без установленного GDI

Этот вариант рассчитан на агент в контейнере провайдера и уже запущенный worker
на компьютере пользователя. Агент получает Git-историю и публикует запросы через
инструменты Google Drive; worker локально выполняет act/Docker и возвращает
консоль, artifacts и result через тот же Drive. Агенту не нужны локальный Docker,
служба worker или credentials пользователя.

Нужны инструменты для поиска/listing папок, скачивания исходных bytes обычных
файлов (включая бинарные `.bundle`/artifacts) и загрузки обычных файлов с сохранением
точных bytes. Поиск по тексту или преобразование JSON в Google Docs не заменяют
эти операции. Сначала проверьте доступные возможности. Если записи на Drive нет,
агент может читать уже созданное задание, но не должен заявлять, что отправил новое.

Пользователь сообщает общий Drive root, путь проекта внутри root, Repository ID,
ветку, точный HEAD и Publication ID, worker ID, profile и выбранные workflow/event.
Например, root `gdi`, проект `my-project`, ref `refs/heads/dev`, worker `user-host`,
profile `full`. `gdrive:`/`rclone:` — локальные имена remotes на компьютере
пользователя; облачный агент находит саму папку Drive своими инструментами.
Пути ниже относительны общему root; `PROJECT_PATH` — путь проекта внутри root.

### Получить уже опубликованный commit

1. Скачайте `PROJECT_PATH/repository.json`; проверьте protocol `version:2`,
   `object_format:"sha1"` и известный Repository ID.
2. Вычислите SHA256 полного ref, например `refs/heads/dev`, как UTF-8 bytes без LF.
   Скачайте `PROJECT_PATH/updates/<ref-sha256>/<publication-id>.json`.
   SHA256 исходных bytes manifest должен совпасть с Publication ID; сверяйте
   `repository_id`, `ref` и `head` с выбранной публикацией.
3. Для полного bundle проверьте `bundle_kind:"full"`, `base_publication:null`,
   `base_head:null` и `prerequisites:[]`. Скачайте
   `PROJECT_PATH/bundles/<bundle_sha256>.bundle`, проверьте SHA256 и `bundle_bytes`.
   Публикация может быть incremental: тогда нужны полный checkpoint и цепочка
   её баз по `base_publication`, применённая по порядку. Не импортируйте дельту
   в пустой clone; порядок и проверки описаны в [docs/protocol.md](docs/protocol.md).
4. Проверьте bundle средствами Git в отдельном временном repository, включая
   фактические prerequisites, полный ref и точный HEAD. Полный bundle проверяется
   в пустом bare repository; затем восстановите рабочий clone и выбранную ветку.
   Для дельт проверяйте наличие prerequisites и ancestry к объявленной базе.
5. Убедитесь, что рабочий HEAD равен выбранному SHA; прочитайте инструкции
   репозитория, включая `AGENTS.md`, из этого commit. Не подменяйте выбранный
   commit более новым tip. Проверка отдельного bundle не заменяет проверку всей
   metadata-цепочки: учитывайте `previous`, отсутствующих предков и конкурирующие
   продолжения по [Git protocol v2](docs/protocol.md).

### Отправить существующую публикацию на локальный CI

Для этой операции новый bundle не нужен. Новый commit сначала должен стать
проверенной Git-публикацией по protocol v2; отправка произвольного SHA в CI request
не заменяет публикацию. Не используйте legacy scripts или `ci/queue` для global
worker v2. Ниже полностью описана отправка уже существующей публикации.

1. Скачайте `ci/workers/WORKER_ID/capabilities.json` из общего root.
   Проверьте `ci_version:1`, `inbox_version:1` и worker ID. Возьмите актуальный
   `profile_revision` из `profiles[PROFILE_ID]`; это 64 lowercase hex.
   Не выдумывайте revision и не запускайте второй worker в облаке.
2. Создайте JOB_ID как `uuid.uuid4().hex` (32 lowercase hex) и `created_at` как
   UTC ISO timestamp с часовым поясом. Подготовьте request с точными полями:

```json
{
  "ci_version": 2,
  "job_id": "JOB_ID",
  "repository_id": "REPOSITORY_ID",
  "ref": "refs/heads/BRANCH",
  "head": "HEAD",
  "publication_id": "PUBLICATION_ID",
  "worker_id": "WORKER_ID",
  "profile_id": "PROFILE_ID",
  "profile_revision": "PROFILE_REVISION",
  "created_at": "UTC_TIMESTAMP",
  "retry_of": null,
  "workflow": {
    "path": ".github/workflows",
    "event": "workflow_dispatch",
    "job": "",
    "inputs": {}
  }
}
```

Это шаблон: подставьте проверенные значения, а не буквальные placeholders.
`workflow` выбирает tracked YAML из указанного commit: `path` — файл или каталог,
`job:""` — без выбора отдельного job, `inputs` — строки. Выбирайте событие,
поддерживаемое workflows; `workflow_dispatch` подходит для явного запуска, если
оно объявлено в YAML. Для поведения push используйте `event:"push"`.
При известном GitHub origin добавьте необязательное поле `github_repository`
с `owner/repository` без URL и credentials. Другие поля request не добавляйте.

Все создаваемые JSON сериализуются одинаково:

```python
def encode(obj):
    import json
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
```

3. До upload сохраните локально JOB_ID и точные request bytes. Вычислите
   REQUEST_SHA256 от этих bytes. Сохраните `request.ready` с точными полями
   `{"ci_version":2,"job_id":"JOB_ID","request_sha256":"REQUEST_SHA256"}`.
   Подготовьте canonical bytes уведомления:

```json
{
  "inbox_version": 1,
  "event_id": "JOB_ID",
  "type": "ci_requested",
  "repository_id": "REPOSITORY_ID",
  "repository_path": "PROJECT_PATH",
  "ref": "refs/heads/BRANCH",
  "head": "HEAD",
  "publication_id": "PUBLICATION_ID",
  "worker_id": "WORKER_ID",
  "job_id": "JOB_ID",
  "request_sha256": "REQUEST_SHA256"
}
```

4. Вычислите EVENT_SHA256 от точных bytes уведомления. Загрузите обычные файлы
   строго по порядку, без перезаписи чужих файлов и без преобразования в Docs:

```text
PROJECT_PATH/ci/jobs/JOB_ID/request.json
PROJECT_PATH/ci/jobs/JOB_ID/request.ready
inbox/JOB_ID-EVENT_SHA256.json
```

Уведомление общей inbox публикуется последним. Перед его публикацией убедитесь,
что предыдущие файлы доступны с ожидаемыми bytes. При сетевом сбое допубликовывайте
те же bytes с тем же JOB_ID; не создавайте новый job для повторной загрузки.
Сохраните/сообщите JOB_ID сразу после отправки. Если локальная сессия потеряна,
сначала проверьте ранее отправленный job: новый UUID может вызвать повторный CI.

### Читать прогресс и проверить результат

Путь задания: `PROJECT_PATH/ci/jobs/JOB_ID/`. Периодически читайте `status.json`
и новые `log-chunks/<sequence:08d>-<sha256>.bin`; сохраняйте исходные bytes,
проверяйте hash имени и порядок sequence с 1 без gaps/дубликатов. Отсутствие
`result.json` означает, что терминальный результат ещё не опубликован.
Advisory status и отсутствие прогресса сами по себе не доказывают PASS/FAIL.

После появления `result.json` выполните проверки как клиент GDI:

- Сверьте `ci_version`, job/repository/worker/profile IDs, ref, HEAD,
  Publication ID и profile revision с request; `request_sha256` должен совпасть
  с hash сохранённых request bytes. Проверьте связь request.ready с request.
- Скачайте все объявленные artifacts. Descriptors должны быть уникальными,
  с `complete:true`, ожидаемыми `bytes` и SHA256. Разрешены `build.log`,
  `final-status.json` и безопасные `artifacts/<name>`; log и final status обязательны.
- Сверьте `final-status.json` с result по job ID, run ID, state и request SHA256.
- Конкатенация всех проверенных log chunks должна совпасть с полным `build.log`
  по размеру и SHA256. Если файлы ещё недоступны, повторите чтение, не объявляя PASS.
- PASS требует `exit_code:0`, непустой список stages, хотя бы одну blocking stage
  и `state:"PASS"`/`exit_code:0` у каждой blocking stage. Проверяйте схему и
  terminal states по [docs/ci-protocol.md](docs/ci-protocol.md).

Сообщите точный HEAD, JOB_ID, выбранные workflows/profile/revision, итог CI,
результаты проверок и существенные warnings. При FAIL прочитайте полный log.
Исправления разрешены только в рамках поставленной задачи: новый commit требует
новой Git-публикации, затем нового CI request. Явный повтор завершённого задания
использует новый JOB_ID и `retry_of` со старым ID; timeout ожидания агента не
отменяет работающий CI. Не отправляйте повтор только из-за задержки upload result.

Worker остаётся запущенным после PASS/FAIL. Облачный агент не создаёт `stop.request`,
не останавливает службу и не меняет credentials/host config. Пользователь получает
проверенный commit через свой установленный GDI:

```bash
gdi pull drive --passed --job JOB_ID --profile full
```
