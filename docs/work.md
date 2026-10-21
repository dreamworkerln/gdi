# Work checkpoints, squash import и retention

## Активация

Work-ID назначает оператор/coordinator; он уникален в логическом GDI-репозитории,
независим от TODO-ID/CI job ID и никогда не переиспользуется. Агент работает через
коннектор, оператор — через обычный CLI. [Инструкция агента](agent.md) каноническая.

Базовый work/import работает поверх Git protocol v3. Для подтверждённого порядка
импортов и retirement включите lifecycle **перед рабочими импортами**, после обновления
всех клиентов/worker/runtime и согласованной паузы обмена:

```bash
gdi remote enable-work drive --quiescent --all-clients-updated
```

В `repository.json` добавляется `work_version: 1`, сохраняются version 3, Repository
ID, manifests и история. Старые клиенты, не понимающие optional field, отказываются
работать: это согласованный несовместимый режим. Обновите их; не пересоздавайте remote
по старому сообщению о неизвестной metadata. Source update сам remote не мигрирует.
Активация повторяема; quiescent подтверждение не является distributed lock.

## Контейнерный агент

После clone исходной ветки и свежего snapshot:

```bash
python3 -m gdi agent work start "$WORK_ID" --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/assigned-target/snapshot.json" --repository-id "$REPOSITORY_ID"
```

Команда создаёт `gdi-work/<WORK_ID>` от фактической выданной ветки. Примерно каждые
10 минут активной работы агент явно проверяет diff/stages и вызывает:

```bash
python3 -m gdi agent work checkpoint --repo "$WORK_DIR" \
  --repository-id "$REPOSITORY_ID" -m 'feat: итог работы' --body-file /path/body.txt
```

Это создаёт `[WORK.PART]`, а `--final` — `[WORK.PART!]`. На обычной ветке checkpoint
запрещён. После materialization `agent work status` восстанавливает next_part из
Git, без локального файла первого start. Work binding включает Repository ID,
неизменяемые target ref и BASE. Часть 10 следует за 9; gaps, duplicate/mixed IDs,
merge/foreign commits и продолжение после `!` запрещены.

Публикация неизменна: **agent prepare → upload bundle → check → upload manifest →
accept**. Snapshot относится к work ref, не к прежней target. Agent не использует
rclone или операторские команды и не воспроизводит протокол вручную. Только
подтверждённая публикация защищает checkpoint от потери checkout.

CI промежуточного checkpoint возможен без `!`: существующий `agent ci prepare`
использует его Publication ID, далее штатные check/accept/result. Он проверяет
точный опубликованный SHA, даже если work ref уже продвинулся. Новый исходный
checkpoint после FAIL — новый CI request, не retry прежнего SHA. CI запускается
по согласованному разрешению, не автоматически каждым checkpoint. Workflows должны
допускать выбранный event и work ref; GDI не подменяет branch для обхода filters.

Финальный body должен честно указывать checkpoint проверок. Не переносите PASS
старого кода на изменённый tree. Metadata-only final seal не превращает проверку
предыдущей части в проверку нового SHA: явно фиксируйте tested SHA и равенство tree,
если такой gate допускается политикой проекта. Gate exact-final-SHA нельзя объявить
пройденным без соответствующего запуска; общий GDI не определяет за проект его gates.

## Импорт оператором

В чистом checkout исходной target:

```bash
gdi import
```

Только готовые `gdi-work/` с последним `!` и валидной цепочкой для этой target:
ноль — причины; одна — сразу выбирается; несколько — меню в TTY, иначе
`selection_required`/список. Явный выбор:

```bash
gdi import --work 19 --json
```

Возвращается pinned план, без изменения target. Применение:

```bash
gdi import --apply /path/from/plan_path.json
```

Закреплены connection/URL/repository identity, target ref/HEAD, source ref/final SHA/
publication, BASE, canonical message и tree merge. Изменение ветки/HEAD/source требует
нового плана. Dirty/detached/in-progress состояния запрещены. Один import coordinator
сериализует импорты всего repository, включая разные target: import receipts имеют
общий последовательный order. На каждом ref — один writer; в checkout не должно быть
сторонних Git writers. Flock не distributed lock; конфликт order означает отказ.

Git готовит squash в отдельном временном worktree. Конфликт оставляет checkout
оператора без изменений, требует отдельного ручного разрешения. Успех добавляет
один single-parent commit поверх target, сохраняя операторские commits после BASE.
Сообщение — полный финальный subject/body, удалён только `[WORK.PART!]` из subject.
Work-история не переписывается и не force-push-ится. PASS work SHA не заменяет
проверку интеграции на новом target, если она обязательна.

В исходной work-ветке создаётся пустой `[GDI-IMPORTED WORK_ID]`, parent — FINAL,
tree прежний. Marker содержит final, target, squash и parent squash. Далее:

```bash
gdi push
```

CLI сначала подтверждает target publication, затем публикует marker без переключения
checkout. При enabled lifecycle подтверждает immutable `work/imports/<WORK_ID>.json`:
связи SHA/publications, время подтверждения и сквозной порядок импорта. Отдельной Git
ветки receipts нет. Результат push и источник отдельно разрешённого `push --ci` остаются
целевой публикацией, не служебным marker.

До применения durable journal сохраняет конкретные squash/marker objects; hidden
refs защищают их от локального GC. Повтор `--apply` того же плана завершает ту же
операцию. После lost response повтор push завершает delivery. Если потерян operator
checkout после target push, но до marker, восстановите target обычным GDI: новый
plan распознаёт существующий squash по точному сообщению, parent и tree трёхстороннего
merge и не создаёт второй commit. Неоднозначность — отказ, не догадка.

## Пылесос

При enabled lifecycle обычный `gdi gc` планирует bundle cleanup и work retirement.
По умолчанию сохраняются **10** последних подтверждённых импортированных work-веток;
N задаётся для конкретного remote через CLI. N=0 убирает все подходящие работы.
Число full bundles внутри ветки по-прежнему задаётся отдельно `--keep-checkpoints`.

```bash
gdi gc drive --keep-imported 5
gdi gc drive --keep-imported 5 --apply --quiescent
```

Сначала — dry run. Apply только после согласованной остановки push/fetch/pull/GC
и workers всех клиентов. Online/background deletion после push не вводится.
`--bundle-only` выполняет прежний bundle GC без retirement work-веток. На remote без
work_version=1 прежний GC сохраняет прежнее поведение; `--keep-imported` требует
активации. Выбранный N можно сохранить в пользовательском alias средствами `gdi alias`.

Порядок — подтверждённые импорты, не Work-ID и не часы контейнера. Без published
squash+marker и immutable import receipt ветка не кандидат. Активные/ready работы,
ordinary branches, новые commits после marker и неподтверждённые импорты сохраняются.
Active/incomplete CI, pending inbox notifications выбранных работ и изменение snapshots
блокируют apply; число сохранённых веток
может временно превышать N.

Перед удалением проверяются сохраняемые histories/bundles с нуля общим GC verifier,
published target с squash, сообщения/parents и marker. Затем:

1. Immutable `work/retired/<WORK_ID>.json` фиксирует retirement до первого удаления.
2. Удаляются только перечисленные manifests выбранной work-ветки.
3. На Drive удаляется её **пустая** папка `branches/<encoded-gdi-work-ref>/` через
   rmdir, без recursive purge и без удаления общего project root.
4. Удаляются только bundles, не нужные сохраняемым веткам/CI архивам.

Общий inbox GC не изменяет: перед паузой дайте worker штатно обработать уведомления
выбранных work refs. Иначе apply остановится до retirement; после обработки повторите
паузу и GC. Это включает repository_updated и CI notifications, не удаляемые collector.

Tombstone остаётся: старый checkout не может воскресить ref/Work-ID. После сбоя
между удалениями оставьте клиентов на паузе и повторите тот же GC с нужным N.
Остаточные manifests сверяются с retirement record; неизвестные/изменённые файлы
вызывают отказ. После apply проверяются удаление папок и состояние metadata/bundles/CI.

Если CI jobs ссылаются на work ref, retirement сохраняет проверяемый архив его
manifest chain в metadata и защищает нужные bundles. Папка ветки всё равно удаляется;
CI result/read/retry и холодное восстановление exact source SHA работают через архив.
CI results/artifacts не удаляются этим collector. Их отдельная retention — TODO_002.
Без CI metadata tombstone содержит только IDs прежних manifests и необходимые связи;
исходная work-история может быть освобождена после доказанной доставки squash.

## Validation и публикация

Software checks и exact checkpoint/evidence записываются в
[TODO_004](../todos/TODO_004_WORK_CHECKPOINT_IMPORT_RETENTION.md).
Обычный workflow/unit discovery подбирает новые tests; real Drive/service acceptance
не запускается автоматически. Обновление `gdi.pyz` и runtime/agent.md требует отдельно
запрошенного commit/push/deploy. Изменение исходной инструкции само runtime не обновляет.
