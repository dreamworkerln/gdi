# TODO_004 — Work checkpoints, squash import и retention

Status: IMPLEMENTED / RUNTIME 435df3c PUBLISHED / INIT-DEFAULT CHANGE NOT VALIDATED.
Implementation: DONE. Validation: 401 software checks PASS для 435df3c; новая правка
инициализации после него — tests NOT RUN. Service/real Drive work-retirement NOT RUN.
Created: 2026-10-21. Updated: 2026-10-22 (Europe/Moscow).
Текущая правка default initialization: `dev@435df3c7fd01a1cb6bb3b38b2e8565fe71568ec3`
+ незакоммиченные source/tests/docs. Исходная реализация и сохранённые software evidence:
`dev@20c6359de147a0dc66f542d3a467a24685020a39` + patch из evidence ниже. Work-ID независим от этого TODO-ID и CI job ID.

## Источник и задача

Оператор согласовал отдельную work-ветку агента, checkpoints примерно раз в 10 минут,
финальный `!`, squash в исходную target, пустой последний `GDI-IMPORTED` в work-ветке
и хранение N последних импортированных работ с уборкой старейших. Дополнительно:
удалять соответствующую папку на Google Drive; CI должен работать на промежуточных
checkpoints; использовать интерфейс контейнера `gdi agent …`, не операторский CLI.
22 октября оператор явно разрешил несовместимость со старыми клиентами для lifecycle.

Цель: потеря checkout при материализации не теряет подтверждённые checkpoints;
оператор получает один commit на работу, сохраняя собственные commits между BASE
и импортом. Несколько агентов имеют разные work refs, одну target могут делить
последовательно. Не переписывать опубликованную историю и не делать force push.

## Архитектура и scope

- `work.py`: общая Git grammar/binding/parent-chain validation, checkpoint и marker.
- `agent_work.py`: тонкий offline Snapshot adapter; без rclone/local remote config.
- `publication.py`: прежнее общее ядро bundle/manifest/quarantine, дополненное
  одинаковой проверкой зарезервированных work refs для CLI и connector writers.
- `work_import.py`: операторские планы/журнал/сборка squash штатным Git в отдельном
  worktree; application orchestration поверх неизменённого Exchange push API.
- `work_lifecycle.py`: optional capability, подтверждения импортов, retirement schema
  и проверяемые архивы source publication metadata.
- `work_gc.py`: work retention; использует существующие GC snapshot, dependency plan,
  clean restore/fsck verifier и CI guard. Transport имеет только scoped operations
  удаления точного work manifest и пустого work directory, без recursive purge.
- CI использует существующие `agent ci`, worker, request/result и source checkout.
  Нового CI протокола, отдельного runner или ручной пересборки manifests нет.

Non-goals: автоматический GitHub push/deploy, background/online deletion, shared
mutable Git receipt branch, CI artifact retention (это TODO_002), распределённая
блокировка и несколько независимых importers одного repository одновременно.
Один coordinator сериализует импорты во все target этого repository; независимые
агенты могут одновременно публиковать разные work refs.

## Текущий контракт

### Work-ID, parts и финал

Оператор/coordinator выдаёт уникальный положительный целый Work-ID без ведущих нулей.
Авто allocation не вводится. ID не переиспользуется после materialization/retirement.
Ветка: `gdi-work/<WORK_ID>`; BASE и фактическая target выбираются при start, без
зашитого dev/main. Agent start требует clean checkout точного HEAD свежего snapshot
исходной ветки. Existing/retired ID отклоняется.

```text
[19.1] feat: результат
[19.2] feat: результат
[19.3!] feat: окончательный результат
```

Генерируемые поля: `GDI-Work`, `GDI-Repository`, `GDI-Target-Ref`, `GDI-Base`.
Проверяются реальные single-parent links 1..N от BASE, не порядок Drive listings.
Нет gaps/duplicate/mixed binding/foreign/merge commits; `!` единственный и последний.
Номер 10 идёт после 9. После восстановления next_part вычисляется из Git history.
Отдельная публикация каждой части для проверки нумерации не требуется.

Агент явно проверяет diff/stages; checkpoint не делает git add -A и отказывается
работать на обычной ветке, при unstaged/untracked или незавершённой Git операции.
Промежуточное сообщение коротко фиксирует сделано/осталось/реальные checks/ограничения.
NOT RUN/FAIL допустимы, но не выдаются за PASS. Примерно каждые 10 минут активной
работы и перед длинным/рискованным этапом checkpoint публикуется штатным
prepare → upload bundle → check → upload manifest → accept. Только accept подтверждает
сохранность. Full bundle cadence `--checkpoint-every` не заменяет этот интервал.

Финальный `!` ставится после согласованных gates. Полный final subject/body становится
squash message с удалением только `[WORK.PART!]`; intermediate messages не склеиваются.
Точный tested SHA и границы переноса результатов сохраняются честно: metadata-only
final seal не доказывает CI на новом SHA, если проект требует exact-final-SHA gate.

### Интерфейс агента и CI

`gdi agent work start/status/checkpoint`, `--repo`, trusted `--repository-id`;
start дополнительно требует `--snapshot`, checkpoint — `-m`, optional `--body-file`
и `--final`. Агент не использует rclone и операторские import/push в контейнере.
Каноническая инструкция: [docs/agent.md](../docs/agent.md).

CI промежуточной части без `!` запускается по разрешению через прежний
`agent ci prepare/check/accept/result` с её Publication ID и work snapshot.
Worker проверяет закреплённый SHA, даже если ref уже продвинулся. Исправленная часть
после FAIL — новая publication/request; retry той же части не подменяет source.
Workflow должен допускать event/ref; GDI не маскирует work ref под target. Проверка
работы не заменяет CI новой интеграции поверх изменённой target.

### Import и delivery

`gdi import [REMOTE]`: discovery только готовых work refs для текущей target,
Repository ID/BASE/target binding и всех частей. Ноль — причины; одна — автовыбор;
несколько — меню либо JSON list с selection_required, выбор `--work ID`.
Возвращается pinned plan; изменение target — отдельный `--apply PLAN`.

План фиксирует remote URL/identity, source ref/final SHA/publication, target ref/HEAD,
BASE, message и tree native three-way merge. Branch/HEAD/source change — отказ,
dirty/detached/in-progress Git states — отказ. Один import coordinator на repository
(в том числе при разных target) и отсутствие сторонних Git writers в checkout
обязательны; локальный flock не distributed CAS. Сквозной порядок import receipts
выдаётся последовательно; конфликтующие номера приводят к отказу, а не автоисправлению.

Squash собирается в отдельном worktree; конфликт не меняет checkout оператора.
Успех — single-parent commit поверх target и fast-forward штатным Git, без переписывания
операторских commits. До применения сохраняется durable journal и refs prepared
objects. Повторы завершают ту же операцию; неприменённый plan после нового операторского
commit можно безопасно заменить, сохранив прежний journal. Применённый squash не
создаётся второй раз. Потеря operator checkout после target publication восстанавливается
по canonical message, parent и проверенному merge tree; неоднозначность — отказ.

После импорта в source work ref создаётся пустой `[GDI-IMPORTED WORK_ID]`, parent=FINAL,
tree=FINAL. Message связывает final/target/squash/parent squash. Обычный следующий
`gdi push` сначала подтверждает target, затем work marker и import receipt. Source
для отдельно разрешённого CI остаётся target publication. Частичная delivery — pending;
локальный marker недостаточен для cleanup. Отдельной receipt Git-ветки нет.

### Lifecycle и пылесос

Новый пустой remote через `remote add --init` сразу получает `work_version=1`
и каталоги `work/imports/`, `work/retired/`; отдельной активации не требует.
Подключение к существующему remote не меняет его metadata.
Для существующих remote активация только после обновления всех клиентов/worker/runtime:
`gdi remote enable-work NAME --quiescent --all-clients-updated`.
Добавляется `work_version=1` в repository.json, сохраняются version=3, Repository ID
и существующая история. Manifest format прежний. Старые клиенты отвергают unknown
field, а не игнорируют retirement. После deploy lifecycle активирован отдельно; затем по поручению оператора включён
на всех шести remote общего root gdrive:gdi с сохранением Repository IDs.

`work/imports/<ID>.json` — immutable подтверждение target/work publications, SHA,
времени и порядка импорта. `work/retired/<ID>.json` — durable tombstone с зависимостями.
Работы импортированные до activation без receipt автоматически не удаляются.

Для enabled lifecycle `gdi gc NAME` планирует общую bundle/work уборку. Default N=10,
переопределение `--keep-imported N` (N=0 допустим); `--bundle-only` сохраняет прежний
bundle GC. Порядок — confirmed import order, не Work-ID/agent clock. Можно хранить >N,
если validation/CI/pending notifications блокируют безопасную уборку. Dry run без
remote writes; apply только `--apply --quiescent` после согласованной паузы всех
клиентов и workers. Автоматического удаления после push нет.

До deletion проверяются marker/chain, published squash/receipts, сохраняемые refs,
CI source и bundle dependency closure, повторное восстановление/fsck с нуля и snapshots.
Retirement пишется первым; затем удаляются только exact manifests выбранной work-ветки,
её **пустая** папка на Drive через rmdir, затем ненужные bundles. Ordinary refs,
project root и CI results/artifacts не удаляются. После partial failure — оставить
паузу, повторить GC; неизвестные/изменённые файлы и metadata вызывают отказ.

При CI dependency tombstone архивирует проверяемую manifest chain и сохраняет нужные
bundles. Папка ветки удаляется, но CI result/retry и cold source restoration продолжают
работать. Без CI исходная work history может быть освобождена после доставки squash.
Stale clients/connector snapshots не воскрешают retired Work-ID/ref.

Общий inbox **не удаляется и не изменяется GC**. Если там есть уведомления выбранных
work refs, collector ждёт их штатной обработки worker, затем новой паузы и повторного
GC. Это защищает от сообщений, указывающих на уже удалённую metadata. Добавление
GC deletion общего inbox было отклонено automatic approval review; реализован
недеструктивный guard вместо расширения удаления.

## Реализация и validation checklist

- [x] Work binding/parts/start/status/checkpoint и materialization recovery.
- [x] Общее publication validation, operator discovery/select/pinned apply.
- [x] Native squash, canonical message, marker, pending delivery/idempotent recovery.
- [x] Optional lifecycle, confirmed import order, stale client/ID guards.
- [x] Retention, scoped Drive folder deletion, common GC restore/dependency/CI gates.
- [x] CI archived source/result/retry после удаления work directory.
- [x] Agent/operator/protocol/architecture documentation и TODO registry.
- [x] Первый unittest discovery: 403 tests, 388.348 s, OK (10 conditional skips).
- [x] Заключительный локальный run software команд workflow с act 0.2.89/rclone 1.75.1:
  discovery — 404 tests / 397.055 s, OK (5 skips); отдельно Docker — 2 tests /
  65.687 s, OK без skips. Итого 401 выполненная проверка PASS; 3 service opt-in NOT RUN.
- [x] По отдельному поручению commit 435df3c, Git/GDI push и deploy runtime + agent.md;
  файлы проверены обратным скачиванием.
- [x] Активация lifecycle только на remote проекта GDI с сохранением Repository ID;
  свободный worker остановлен перед активацией и запущен после.
- [x] По новому указанию default `work_version=1` при `--init`, документация и regressions.
- [ ] Проверки новой правки initialization: NOT RUN; новый commit/push/deploy не выполнялся.

## Checkpoint/evidence и границы

Workspace evidence: `/home/dream/coding/journal_of_experiments/20261021T203414Z_gdi_work_todo004/`.
Exact software source: `software-source-manifest.json` + `validated-source/`, BASE выше;
tracked diff перед run — `tracked-before-final-validation.patch`. Итоговая сверка
в `completion-summary.json` подтверждает неизменность всех 78 source/test/script files;
полный финальный patch — `final-tracked.patch`, новые файлы — `final-untracked/`.

До финального run: work subset 17 PASS; work+intermediate CI 18 PASS; work GC 27 PASS
(включал повторно импортированный WorkTests class; затем duplicate discovery исправлен);
archive/native/GC subset 12 PASS. Это software fixture/local-backend evidence, не real
Drive или physical/service acceptance. CI tests используют existing fake-act worker,
а native RC/CLI tests — настоящий rclone local backend. Дополнительно запускается
реальный Docker acceptance существующего workflow (2 PASS).

Первый full discovery имел 10 skips из-за optional tool/environment flags. После
preflight доступны act 0.2.89, rclone 1.75.1 и image
`catthehacker/ubuntu:act-latest` ID
`sha256:c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43`.
Заключительные команды и skips записаны в `workflow-*-summary.json`. В discovery
399 выполненных tests PASS; два Docker skips покрыты отдельным успешным run. Три
ServiceV2CliTests требуют явно настроенной acceptance user service и не выполнялись.
Перезапусков после успешного заключительного run не было.
Это локальное исполнение software checks; сервер/GitHub CI нового code commit не
запускался. Отдельный backlog паритета local entrypoint — TODO_001 — не закрывается
этой задачей. На момент software run real Drive/service acceptance, commit/push/deploy
не выполнялись. Последующий deploy 435df3c выполнен и подтверждён:
`deployment-summary.json` в том же evidence-каталоге. Runtime/agent.md опубликованы.
Активация GDI remote сохранена в
`/home/dream/coding/journal_of_experiments/20261021T212200Z_gdi_work_activation/result.json`.
Это не acceptance work-retirement на реальном Drive. Новая правка default initialization
ещё не коммитилась и не публиковалась; тесты для неё не запускались.

## История решений

2026-10-21: TODO-system и план созданы, прежний TODO сохранён полностью; реализация
поручена отдельным сообщением. ST fast-forward до 8a9d91d завершён и ST далее не менялся.
2026-10-22: оператор уточнил interface агента, remote folder cleanup и промежуточный CI;
после отдельного вопроса разрешил несовместимый lifecycle. Первоначальная попытка
ввести его до явного согласия была остановлена auto-review, затем применена после
согласия. Неавторизованное расширение удаления на общий inbox заменено read-only guard.

2026-10-22: после публикации 435df3c и активации GDI remote оператор поручил создавать
новые пустые remote сразу с work_version=1. Existing join/remote не активируются
автоматически. Добавлены regression cases и обновлены connector/legacy fixtures;
tests NOT RUN, только статическая проверка. Новый тематический TODO не требуется.

2026-10-22: по поручению «включай ща все репы» проверены все шесть repository.json
общего root gdrive:gdi. Включены serialterminal, lora-sack, codec2-sdk, codex-uploader
и gdi-ci-smoke-20261010T110004Z; у gdi подтверждён уже включённый режим. Worker был
остановлен при отсутствии pending jobs, затем запущен обратно. GC/fetch/pull/commits
не выполнялись. Before/after metadata и результаты:
`/home/dream/coding/journal_of_experiments/20261021T213229Z_gdi_all_work_activation/result.json`.
