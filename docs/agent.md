# GDI через коннектор Google Drive

Агент запускает GDI из полученных исходников. Нужны Python 3.10+ на Linux и Git;
сторонние Python-пакеты, pip, setuptools, rclone, Docker и worker агенту не нужны.
GDI читает локальные снимки и готовит файлы. Все операции Drive выполняет коннектор.
Форматы Git protocol v3 и CI request v2 остаются общими с обычным клиентом.
Для CI через обычный CLI с rclone используйте [ci.md](ci.md).

В примерах команды запускаются из каталога исходников GDI. `--repo` задаёт рабочий
Git-репозиторий проекта; это может быть другой каталог. Вывод команд — один JSON
на stdout; сообщения о ходе работы идут на stderr. Ошибка возвращает код 1 и JSON
с `kind: GDI_ERROR`. Обычное `gdi agent ci result` возвращает код 0 и для проверенного
FAIL: состояние CI определяется полями `state` и `verified`, а не кодом этой команды.

```bash
INSTALL_DIR="/путь/к/исходникам/gdi"
WORK_DIR="/путь/к/рабочему/my-project"
EXCHANGE_DIR="/путь/к/локальному/обмену"
REPOSITORY_ID="доверенный_repository_id_пользователя"
cd "$INSTALL_DIR"
python3 -m gdi agent --help
```

Храните снимки и планы вне tracked файлов проекта. Для каждой новой передачи
выбирайте отдельный каталог плана. До завершения передачи сохраняйте этот каталог:
повтор использует прежние bytes, nonce и job ID. После ошибки не создавайте новый
план только из-за потери ответа коннектора. При загрузке сначала ищите точное имя
в полном listing: существующий файл с теми же bytes переиспользуется, отличающиеся
bytes или дубликаты имён требуют остановки. Не перезаписывайте immutable файлы.

## Первое получение самих исходников GDI

Если GDI ещё отсутствует, пользователь передаёт доверенный полный Git bundle GDI
и независимо сообщает SHA256, ветку и точный HEAD. Агент может скачать этот bundle
коннектором из GDI-репозитория. Сверьте SHA256, выполните `git bundle verify` в пустом
bare repository и `git bundle list-heads`: должен быть ровно согласованный ref/HEAD,
без prerequisites. Затем восстановите из него рабочий clone с отключёнными hooks
и выбранной веткой. До получения доверенных исходников не запускайте код из bundle.
После этого дальнейшую валидацию снимков и восстановление проектов делает сам GDI.
Пакет через pip устанавливать не требуется; запустите `python3 -m gdi --help`.

## Снимок коннектора

Для Git-публикации скачайте свежие `repository.json` и **все manifests выбранной
ветки**, получив полный listing папки проекта, `branches` и выбранной ветки.
Если ветка отсутствует в полном listing `branches`, её listing не нужен.
Корень проекта должен содержать каталоги `branches` и `bundles`.
Для проверки загруженного bundle дополнительно нужны полный listing `bundles`
и скачанные bytes проверяемого bundle. Для clone — checkpoint и все нужные дельты.

Создайте `snapshot.json` следующего формата. Идентификаторы — реальные непрозрачные
Drive ID, разрешённые коннектором. Здесь показан пустой репозиторий; для существующей
ветки добавьте её папку в listing `branches`, полный listing этой папки и manifests.

```json
{
  "snapshot_version": 1,
  "repository_path": "my-project",
  "repository_folder_id": "PROJECT_FOLDER_ID",
  "ref": "refs/heads/dev",
  "listings": [
    {
      "folder_id": "PROJECT_FOLDER_ID",
      "pages": [{
        "page_token": null,
        "next_page_token": null,
        "entries": [
          {"id": "REPOSITORY_FILE_ID", "name": "repository.json", "is_dir": false, "bytes": 87},
          {"id": "BRANCHES_FOLDER_ID", "name": "branches", "is_dir": true, "bytes": null},
          {"id": "BUNDLES_FOLDER_ID", "name": "bundles", "is_dir": true, "bytes": null}
        ]
      }]
    },
    {
      "folder_id": "BRANCHES_FOLDER_ID",
      "pages": [{"page_token": null, "next_page_token": null, "entries": []}]
    }
  ],
  "files": [{"file_id": "REPOSITORY_FILE_ID", "local_path": "downloads/repository.json"}]
}
```

`bytes` берётся из metadata коннектора и должен совпадать с размером скачанного
файла; число 87 в примере замените фактическим. Папки имеют `bytes: null`.
Имена не декодируйте: `feature%2Flogin` и `%25` на Drive буквальные.
Каждый listing **не рекурсивный**; дочерние папки получают отдельные listings.
Сохраните все entries и страницы, не превращайте имена в словарь: дубликаты должны
попасть в снимок и вызвать отказ GDI. Первая страница имеет `page_token: null`,
каждая следующая использует `next_page_token` предыдущей; только последняя имеет
`next_page_token: null`. Если коннектор возвращает уже полный агрегированный listing,
сохраните его одной страницей с обоими tokens равными null.

`files` связывает скачанные файлы с их Drive ID. `local_path` относителен каталогу
snapshot.json; запрещены абсолютные пути, `..`, symlinks и специальные файлы.
Не редактируйте скачанные JSON и не сериализуйте их заново: проверяются точные bytes.
Свежесть, отсутствие фильтров поиска и полнота listing на самом Drive — обязанность
агента и коннектора. GDI проверяет согласованность предоставленного снимка; tokens
не являются доказательством независимого доступа GDI к Drive.

```bash
python3 -m gdi agent snapshot \
  --snapshot "$EXCHANGE_DIR/before/snapshot.json" --repository-id "$REPOSITORY_ID"
```

GDI проверяет repository identity, schema, SHA256 manifests, всю цепочку `previous`,
ref/HEAD, incremental bases, пропуски и конкурирующие продолжения. Доверенный
Repository ID задавайте из сообщения пользователя, а не копируйте из снимка.

## Получить проект

Ответ `gdi agent snapshot` содержит `required_bundles` в порядке восстановления,
с именами, folder ID, размерами и SHA256. Скачайте именно эти bundles коннектором;
агенту не нужно самостоятельно вычислять dependency chain. Включите listing `bundles`
и их downloads в снимок. GDI восстанавливает tip выбранной ветки в новый каталог:

```bash
python3 -m gdi agent clone --snapshot "$EXCHANGE_DIR/source/snapshot.json" \
  --repository-id "$REPOSITORY_ID" --destination "$WORK_DIR"
```

Каталог назначения должен отсутствовать. Проверка/восстановление проходит во
временном каталоге и quarantine; существующие файлы не заменяются.
При bundle GC достаточно последнего доступного checkpoint и дельт от него до tip;
все manifests цепочки всё равно нужны. Проверяется fast-forward от `previous`.
После clone прочитайте инструкции проекта и внесите изменения. Создание commit
остаётся отдельным действием агента; GDI не коммитит автоматически.

## Передать изменения пользователю

1. Скачайте свежий снимок metadata. В рабочем Git-репозитории уже должен быть
   создан commit согласованной задачи, потомок опубликованного HEAD.
2. Подготовьте публикацию:

   ```bash
   python3 -m gdi agent prepare --repo "$WORK_DIR" \
     --snapshot "$EXCHANGE_DIR/before/snapshot.json" --repository-id "$REPOSITORY_ID" \
     --output "$EXCHANGE_DIR/publication-plan"
   ```

   В JSON будут `files.bundle`, `files.manifest`, `files.notification`:
   `local_path`, `target_folder`, `target_name`, `sha256`, `bytes`;
   `upload_order`, `expected_previous_publication`, `expected_new_tip` и `next_step`.
   `target_folder` — логический путь **от общего Drive root**, не Drive folder ID.
   Найдите/создайте эти папки коннектором. `bundle_kind` показывает выбранный тип:
   по умолчанию передаётся incremental от опубликованного tip. Первая публикация
   и периодический checkpoint используют full, как обычный push: после 19 дельт
   создаётся новый full. `--full` принудительно выбирает полный bundle;
   `--checkpoint-every N` меняет период checkpoints.
   Canonical JSON, nonce, SHA256, ветки и quarantine совпадают с обычным push.
   Для проверки дельты GDI сохраняет `base.bundle` из локальной Git-истории внутри
   плана. Это локальный файл: загружайте только перечисленное в `files`.
   Старые bundles с Drive для prepare/check/accept скачивать не требуется.
   Если HEAD уже опубликован, ответ `already_published` не требует загрузок.
   Исключение — `--full` для уже опубликованной дельты: создаётся полный checkpoint.
3. Загрузите bundle, скачайте его обратно. Обновите metadata и снимок с полным
   listing `bundles` и downloaded bundle. Выполните:

   ```bash
   python3 -m gdi agent check --plan "$EXCHANGE_DIR/publication-plan" \
     --snapshot "$EXCHANGE_DIR/after-bundle/snapshot.json" --repository-id "$REPOSITORY_ID"
   ```

   Только успешный ответ `safe_to_upload_manifest: true` разрешает следующую загрузку.
   Bundle проверяется по bytes/размеру/SHA256 и в quarantine; база должна оставаться
   ровно прежней. Разрешение относится к этому снимку и не блокирует конкурентный push.
4. Загрузите manifest последним. Скачайте его обратно и получите свежий **полный**
   listing выбранной ветки. В новом снимке сохраните ранее скачанные manifests
   и проверенный bundle вместе со свежими listings:

   ```bash
   python3 -m gdi agent accept --plan "$EXCHANGE_DIR/publication-plan" \
     --snapshot "$EXCHANGE_DIR/after-manifest/snapshot.json" --repository-id "$REPOSITORY_ID"
   ```

   Требуется единственный tip, ровно подготовленный Publication ID и точные bytes.
   При конкурентном продолжении/смене базы остановитесь, получите новый снимок,
   согласуйте историю Git и создайте **новый** план; старый сохраните для диагностики.
   При потере ответа manifest мог уже попасть на Drive: сначала повторите accept
   со свежим снимком, не создавайте новую публикацию и nonce.
5. При желании после accept загрузите `files.notification`: это `repository_updated`,
   которое не запускает CI. Для обычного fetch/pull уведомление не обязательно.
   Передайте пользователю Repository ID, ref, HEAD, Publication ID и команду `gdi pull`
   из нужного Git-репозитория на согласованной ветке/подключении.

Повтор prepare с тем же каталогом плана и неизменным HEAD возвращает прежние bytes.
Параметры full/checkpoint применяются только при создании нового плана; retry
существующего плана сохраняет выбранный тип bundle, bytes и nonce.
Повтор check/accept проверяет новый снимок. Проверяемые bundles не требуют скачивания
остального репозитория; скачанные bytes можно оставить локально для следующего accept.

### Минимальный обмен с коннектором

Рабочая последовательность: commit → prepare → upload bundle → check → upload manifest
→ accept. Upload-probe и ручное повторение проверок GDI не нужны.

- Перед prepare получите свежий `repository.json` и полные listings проекта,
  `branches`, выбранной ветки и `bundles`. Скачайте manifests, которых ещё нет локально;
  listing `bundles` используется для поиска подготовленного имени перед upload.
- После upload bundle скачайте обратно только этот bundle. Обновите
  `repository.json` и все перечисленные listings, включая `bundles`.
  Он нужен для проверки дубликатов; остальные bundle-файлы скачивать не требуется.
- После upload manifest скачайте обратно только этот manifest. Ещё раз обновите
  `repository.json` и полные listings проекта, `branches`, выбранной ветки и `bundles`.
  Ранее проверенный bundle и неизменные manifests используйте из локальных файлов.

Каждый свежий listing получайте со всеми страницами один раз за этап; этот же
listing используйте для поиска файла, проверки имён и сборки снимка. Если коннектор
поддерживает пакетное получение независимых listings/downloads, группируйте их.
Перед upload используйте точное имя из плана и полный listing папки назначения.
Если файл уже существует, скачайте и сравните SHA256/размер, затем переиспользуйте
совпадающие bytes. При дубликатах или отличающихся bytes остановитесь; потеря ответа
upload требует повторной проверки существующего файла, а не создания ещё одного.
Сохранённые Drive folder IDs используйте после проверки связей со свежим listing
родителя. Переиспользовать bytes можно только для прежних immutable имён/IDs/размеров;
GDI проверит hashes и полную цепочку. `repository.json` и mutable CI metadata получайте
заново, а не из старого локального снимка.

Для нового снимка скопируйте сохранённые downloads в его каталог обычной копией
или hardlink: пути `..` и symlinks не поддерживаются. Свежие listings обязательны
даже при локальном переиспользовании bytes: они выявляют удаление, дубликаты и
конкурирующую публикацию. При изменениях следуйте ошибке GDI и обновите снимок.

## Подготовить запрос CI

Делайте это только по явному указанию пользователя. Простая публикация изменений
не означает согласия запускать CI. Worker, act и Docker находятся на компьютере
пользователя. Агент только готовит и передаёт immutable файлы.

Скачайте свежие `ci/workers/WORKER_ID/capabilities.json` из **общего root**, получите
свежий снимок проекта с проверенной публикацией. Workflows должны присутствовать
в точном опубликованном commit; локальная ветка может быть уже впереди него.

```bash
python3 -m gdi agent ci prepare --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/published/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --publication PUBLICATION_ID --worker user-host --profile full \
  --capabilities "$EXCHANGE_DIR/capabilities.json" \
  --output "$EXCHANGE_DIR/ci-plan"
```

Поддерживаются те же `--workflow`, `--event`, `--job`, повторяемый `--input KEY=VALUE`.
План содержит `files.request`, `files.ready`, `files.inbox` и фиксированные job ID,
profile revision, ref/HEAD и Publication ID. Порядок: **request → ready → inbox**.
Повтор prepare с тем же планом сохраняет job ID/bytes. Изменение capabilities
требует нового явного запроса; при простой задержке worker новый job не создавайте.

1. Загрузите request.json, затем request.ready в `PROJECT_PATH/ci/jobs/JOB_ID/`.
2. Скачайте оба обратно, свежие capabilities и metadata проекта. Дополните снимок
   listings `ci`, `ci/jobs`, `ci/jobs/JOB_ID` и downloads request/ready.

   ```bash
   python3 -m gdi agent ci check --repo "$WORK_DIR" --plan "$EXCHANGE_DIR/ci-plan" \
     --snapshot "$EXCHANGE_DIR/ready/snapshot.json" --repository-id "$REPOSITORY_ID" \
     --capabilities "$EXCHANGE_DIR/capabilities.json"
   ```

   Inbox загружайте только после `safe_to_upload_inbox: true`.
3. Загрузите `files.inbox` в общий `inbox`, скачайте обратно до удаления worker.
   Сохраните `inbox-proof.json` — полный listing папки inbox и download:

   ```json
   {
     "proof_version": 1,
     "folder_id": "INBOX_FOLDER_ID",
     "pages": [{"page_token": null, "next_page_token": null, "entries": [
       {"id": "EVENT_FILE_ID", "name": "JOB_ID-EVENT_SHA256.json", "is_dir": false, "bytes": 456}
     ]}],
     "file_id": "EVENT_FILE_ID",
     "local_path": "downloaded-event.json"
   }
   ```

   Имена/bytes замените фактическими; pages сохраняются по тем же правилам.

   ```bash
   python3 -m gdi agent ci accept --repo "$WORK_DIR" --plan "$EXCHANGE_DIR/ci-plan" \
     --snapshot "$EXCHANGE_DIR/ready/snapshot.json" --repository-id "$REPOSITORY_ID" \
     --capabilities "$EXCHANGE_DIR/capabilities.json" --inbox-proof "$EXCHANGE_DIR/inbox-proof.json"
   ```

   Если worker уже забрал событие, проверьте результат/прогресс существующего job;
   исчезновение уведомления не означает сбой запроса. Не создавайте новый job.

## Проверить результат CI

Скачайте request, ready, result, final status, полный build.log, все перечисленные
artifacts и **все** log chunks. Для каждого необходим полный listing родительской
папки; включите их в снимок проекта. Events и mutable status не подтверждают PASS.
Папка log-chunks включается, если она присутствует; пустой лог может не иметь chunks.

```bash
python3 -m gdi agent ci result --repo "$WORK_DIR" --plan "$EXCHANGE_DIR/ci-plan" \
  --snapshot "$EXCHANGE_DIR/result/snapshot.json" --repository-id "$REPOSITORY_ID"
```

Используется тот же проверяющий код, что у обычного CI-клиента: identity запроса,
результата и final status, SHA256/размер каждого artifact, непрерывность chunk sequence,
совпадение chunks с полным binary log и успешные blocking stages для PASS.
`PENDING, verified:false` означает, что result ещё отсутствует в полном listing.
Скачанные artifacts проверенного результата сохраняются в каталоге плана.

Для явного повторного исполнения завершённого задания: новый каталог плана и
`ci prepare --retry-of OLD_JOB_ID`, с downloads полного проверяемого старого result
в снимке. Активное задание повторять нельзя. Повтор передачи прежнего плана —
повтор доставки того же job, а не новое исполнение.
