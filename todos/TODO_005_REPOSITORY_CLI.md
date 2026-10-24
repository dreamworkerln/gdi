# TODO_005 — Локальный init, online clone и явное создание remote

Дата: 2026-10-24. Источник: согласованный запрос оператора в текущем чате;
реализация, tests, commit и push в GitHub/GDI разрешены отдельными сообщениями.
Исходный checkpoint: `134a12574f6a3f81b7cec92e8d0207f286f4f5ad`.
Commit реализации до интеграции: `03bf4f6` (GDI 0.4.0).
Интегрируемый опубликованный checkpoint: `b5886b0731e3bfadc576e18e7f38f85e4b38f4b1`.

Status: CLOSED. Implementation: DONE. Validation: PASS для согласованного software
CLI контракта на BASE b5886b0 + source patch ниже. Полный discovery: 434 tests,
429 PASS и 5 явно включаемых Docker/service SKIP. Live Drive/service acceptance
не является gate этой задачи и не объявляется выполненным. Исторический PASS ниже
относится к исходной реализации до merge; актуальный результат записан отдельно.

## Scope и зависимости

Обычный CLI создания/подключения репозиториев; `agent work`, import, lifecycle,
checkpoint interval 10 и прочие входящие изменения dev сохраняются. Новый remote
через `remote create` сохраняет установленный в b5886b0 default `work_version=1`.
Существующие remote не мигрируются; новая публикация сама не возвращает capability,
снятую оператором 2026-10-24. Runtime, службы, чужие ветки и фоновые очистки не входят
в задачу. Данные, repository ID и Git/config protocol versions сохраняются.

## Согласованный контракт и первоначальная реализация

## Обычный CLI: init, clone и явное создание remote — DONE (2026-10-24)

Запрос оператора 2026-10-24: сделать создание и подключение репозиториев в обычном
CLI понятными по аналогии с Git. Разделить локальную инициализацию, создание remote,
подключение существующего remote и отправку commits. Это задача обычного CLI,
не изменение команд `gdi agent ...`.

Согласованный интерфейс:

- [x] `gdi init [папка]` — создать локальный Git-репозиторий и `.gdi`; если
  Git-репозиторий уже существует, добавить локальную конфигурацию GDI с сохранением
  существующих branch/HEAD/index/worktree и подключений. Ручной `git init` перед
  этой командой не нужен. Команда не создаёт commits и ничего не создаёт на Drive.
- [x] `gdi clone URL [папка]` — получить существующий удалённый GDI-репозиторий:
  подготовить локальный Git и `.gdi`, проверить и скачать историю, сделать checkout
  и сохранить подключение `drive`. Ручные `git init` и `remote add` после clone
  не нужны. Предусмотреть `--branch` для явного выбора опубликованной ветки;
  определять выбор без этого флага явно, не угадывая ветку при неоднозначности.
- [x] `gdi remote add ИМЯ URL` — сохранить локальное подключение к существующему
  remote; не создавать и не переинициализировать удалённый репозиторий.
- [x] `gdi remote create ИМЯ URL` — явно создать пустой GDI-репозиторий на Drive
  и сохранить локальное подключение. Создание допускается только по отсутствующему
  адресу либо в пустой выделенной папке; существующие данные не переписываются.
- [x] Убрать создание remote через `gdi remote add ИМЯ URL --init` и заменить
  этот пользовательский сценарий на `gdi remote create ИМЯ URL`.
- [x] `gdi push [ИМЯ]` — отправлять committed историю в уже созданный remote.
  Push не создаёт remote и не создаёт commits автоматически. Если remote ещё
  не создан, завершаться с понятной подсказкой `gdi remote create ИМЯ URL`.
- [x] Сохранить команды `remote list/remove/default`, `fetch` и `pull`, проверку
  формата и repository ID, ограничения целевого каталога clone и сохранность
  существующих локальных и удалённых данных при отказах и прерываниях.
- [x] Обновить help, README, INSTALL, QUICKSTART и актуальные описания обычного
  CLI. Добавить проверки новых команд, отказа создания поверх непустого remote,
  неверного repository ID, выбора ветки, занятого каталога clone и отсутствия
  удалённых изменений от `init`/`remote add`/неуспешного `push`.

Примеры целевого интерфейса:

```bash
gdi init my-project
cd my-project
# Добавить файлы и создать commit обычными git add / git commit.
gdi remote create drive rclone:gdi/my-project
gdi push

gdi clone rclone:gdi/lora-sack --branch dev_chat_binary
```

Для существующего локального Git-проекта: `gdi init`, затем
`gdi remote add drive URL` для уже существующего remote либо
`gdi remote create drive URL` для нового, затем обычный обмен.

Критерий закрытия: новый локальный проект и получение существующего удалённого
проекта доступны через обычный CLI без ручного `git init`; создание remote выполняет
отдельная явная команда, `remote add` только подключает, `push` только публикует
commits в существующий remote. Сценарий `remote add --init` заменён в CLI и документации.
Реализовано в `gdi/repository.py`, `gdi/cli.py` и `gdi/exchange.py`. Init исключает
`.gdi` через локальный `.git/info/exclude`; существующие Git-состояние и подключения
сохраняются. Clone получает все опубликованные ветки, единственную выбирает
автоматически, при нескольких требует `--branch`; пустой remote тоже поддерживается.
Данные проверяются в соседнем временном каталоге до установки checkout. Цель может
быть новой или пустой, но не текущим каталогом оболочки; существующие файлы и
символьные ссылки не перезаписываются. Форматы Git protocol v3 и local config v2
сохранены, команды `agent` не менялись. README, INSTALL, QUICKSTART, help и связанные
описания обычного CLI обновлены.

Validation 2026-10-24: `bash test.sh` — OK, 394 tests: 389 PASS, 5 SKIP
(2 Docker tests без `GDI_TEST_DOCKER`, 3 явно включаемых service acceptance tests).
В общий discovery включены 28 проверок `tests/test_repository.py`, в том числе
настоящий цикл CLI/local rclone и native RC. Реальный Drive и пользовательская
служба для этой задачи не изменялись и в acceptance не проверялись.
`git diff --check` — PASS. Implementation: DONE. Commit/push/deploy: NOT RUN.

## Интеграция с опубликованной dev

2026-10-24: перед push обнаружены четыре входящих commits, общий опубликованный HEAD
GitHub и GDI — b5886b0. Локальная реализация сохранена отдельным commit, выполняется
обычный merge без rebase/force push. Входящая политика TODO требует отдельный файл
и реестр; задача перенесена из TODO.md с сохранением первоначального контракта.

Закрытый gate: тесты объединённого source tree и сверка сохранности work/import
и нового CLI; 127 targeted checks и полный discovery прошли. После PASS сохранить source patch относительно b5886b0 и лог проверки
в `todos/evidence/TODO_005/`, записать SHA256 patch и обновить реестр. Docker/service
acceptance остаются явно включаемыми проверками; они не являются gate этого CLI
контракта. Реальный пользовательский Drive не используется для тестовых fixtures.

## Проверяемая source revision merge

BASE: `b5886b0731e3bfadc576e18e7f38f85e4b38f4b1` +
[сохранённый source patch](evidence/TODO_005/merge-source.patch).
SHA256: `7fd721fd7b6034ab044b63396f0010ba99026e7e7a5c14200ad6079144bc21e9`. Patch фиксирует gdi/, tests/ и package version;
последующие правки реестра/evidence не меняют проверенный source tree.

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_repository tests.test_exchange tests.test_work tests.test_work_gc -v`
— 127 PASS, лог: [merge-focused-tests.txt](evidence/TODO_005/merge-focused-tests.txt).
Полный `bash test.sh` на том же source patch: OK, 434 tests, 429 PASS, 5 SKIP.
Лог: [merge-full-tests.txt](evidence/TODO_005/merge-full-tests.txt);
[окружение и итог](evidence/TODO_005/validation-summary.txt).

Patch сохранён без context lines; read-only проверка соответствия текущему source:
`git apply --check --reverse --unidiff-zero todos/evidence/TODO_005/merge-source.patch`.

## Итог software приёмки 2026-10-24

Согласованные init/clone/remote create реализованы, прежний `remote add --init`
отвергается. Work/import/retention, interval 10 и capability default входящего
b5886b0 сохранены; существующие remote не мигрируются. README/help и связанные
инструкции согласованы. Merge-конфликты сняты, определения прежних функций
сохранены, `git diff --check` и staged diff check проходят. Для публикации оператор
отдельно поручил normal push GitHub/GDI; runtime deploy не входит в поручение.
