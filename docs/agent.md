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

## Обязательный способ работы с GDI-репозиториями

Для любого GDI-репозитория используйте существующие команды GDI. Это правило
распространяется и на репозиторий самой утилиты: установленный доверенный GDI
получает новую версию GDI с Drive. Не заменяйте интеграцию собственными скриптами
или ручным воспроизведением протокола.

- Проверка снимка, выбор bundles и восстановление: `gdi agent snapshot`
  и `gdi agent clone`.
- Публикация: `gdi agent prepare`, `gdi agent check`, `gdi agent accept`.
- CI: `gdi agent ci prepare`, `check`, `accept`, `result`.

Коннектор выполняет listings, скачивание и загрузку файлов; агент собирает описанный
ниже снимок из этих listings и downloads и передаёт его GDI. Скачивайте bundles
по `required_bundles`, загружайте файлы по проверенному плану и соблюдайте решения
GDI о следующих шагах. Проверку identity, hashes, manifests, зависимостей bundles,
git objects и результатов CI выполняет GDI.

Не выбирайте checkpoint и дельты вручную, не собирайте из них репозиторий командами
`git bundle create/unbundle`, `git clone <bundle>` или `git fetch <bundle>`.
Не создавайте и не исправляйте вручную bundles, manifests, Publication IDs,
CI request/ready/inbox/result. Не пишите собственную реализацию их проверок вместо
команд GDI. Обычные git-команды для работы с исходниками и просмотра локального
HEAD/ветки разрешены; восстановление из файлов GDI-протокола делает `agent clone`.
При ошибке GDI исправьте указанные входные данные или получите свежий снимок;
если продолжение невозможно, сообщите причину, сохранив файлы и план.

Единственное исключение — первоначальный bootstrap, когда доверенной работоспособной
копии GDI ещё нет: используйте только процедуру следующего раздела. Наличие этого
исключения не разрешает ручную сборку при обычном обмене или обновлении уже
работающего GDI.

## Первое получение самих исходников GDI

Если доверенной копии GDI ещё нет или она не поддерживает протокол репозитория
утилиты, для bootstrap нужны только Python, git и коннектор Drive.

1. Получите от владельца источник доверенного **полного** bundle самой утилиты:
   Drive file ID или однозначное расположение, SHA256, имя ветки и полный HEAD.
   Ожидаемые SHA256/HEAD должны прийти независимо от скачанного файла; не назначайте
   вычисленный hash доверенным только потому, что скачивание удалось. Если этих
   сведений нет, запросите их. Для дальнейшего обмена также сохраните доверенный
   Repository ID и папку GDI-репозитория утилиты.
2. Скачайте этот bundle коннектором в локальный файл. Не выбирайте произвольную
   дельту и не пытайтесь вручную собирать цепочку prerequisites. Полный checkpoint
   может быть старее tip ветки: его достаточно для первого запуска, после которого
   сам GDI получает более свежую опубликованную версию по следующему разделу.
3. Задайте фактические параметры. Bundle должен иметь абсолютный локальный путь;
   каталоги проверки и установки должны ещё не существовать, а их родительские
   каталоги — быть доступными для записи.

   ```bash
   BOOTSTRAP_BUNDLE="/путь/к/скачанному/полному.bundle"
   BOOTSTRAP_SHA256="ожидаемый_sha256_от_владельца"
   BOOTSTRAP_BRANCH="ветка_из_доверенных_сведений"
   BOOTSTRAP_HEAD="полный_sha_commit_от_владельца"
   BOOTSTRAP_VERIFY_DIR="/путь/к/новому/bootstrap-verify.git"
   INSTALL_DIR="/путь/к/новой/локальной/копии/gdi"
   ```

4. Проверьте файл и восстановите первую копию. Все команды в этом блоке должны
   завершиться успешно; `set -eu` останавливает bootstrap при первой ошибке.

   ```bash
   (
     set -eu
     test ! -e "$BOOTSTRAP_VERIFY_DIR"
     test ! -e "$INSTALL_DIR"
     printf '%s  %s\n' "$BOOTSTRAP_SHA256" "$BOOTSTRAP_BUNDLE" | sha256sum --check --strict
     git -c core.hooksPath=/dev/null init --quiet --bare --template= "$BOOTSTRAP_VERIFY_DIR"
     git -C "$BOOTSTRAP_VERIFY_DIR" bundle verify "$BOOTSTRAP_BUNDLE"
     test "$(git -C "$BOOTSTRAP_VERIFY_DIR" bundle list-heads "$BOOTSTRAP_BUNDLE")" = \
       "$BOOTSTRAP_HEAD refs/heads/$BOOTSTRAP_BRANCH"
     git -c core.hooksPath=/dev/null clone --quiet --template= \
       --branch "$BOOTSTRAP_BRANCH" "$BOOTSTRAP_BUNDLE" "$INSTALL_DIR"
     test "$(git -C "$INSTALL_DIR" rev-parse HEAD)" = "$BOOTSTRAP_HEAD"
     test "$(git -C "$INSTALL_DIR" symbolic-ref --short HEAD)" = "$BOOTSTRAP_BRANCH"
     cd "$INSTALL_DIR"
     python3 -m gdi --version
     python3 -m gdi agent --help
   )
   ```

   Проверка bundle в пустом bare repository отклонит файл с отсутствующими
   prerequisites; проверка `list-heads` требует ровно согласованный ref/HEAD.
   Если передана инкрементальная дельта, `git bundle verify` сообщит
   `Repository lacks these prerequisite commits` и bootstrap остановится до clone
   и запуска GDI, даже при правильном SHA256. Сообщите владельцу, что для bootstrap
   нужен полный bundle; он может опубликовать его командой `gdi push REMOTE --full`
   из репозитория утилиты. После получения полного bundle используйте новый пустой
   каталог проверки. Не пытайтесь вручную догружать bases и обходить отказ.
   До этих проверок не запускайте код скачанной утилиты. Установка через pip,
   rclone и Docker для такого запуска не нужны.
5. Прочитайте `AGENTS.md` и `docs/agent.md` восстановленной копии, затем запускайте
   команды из `INSTALL_DIR`. Bootstrap завершён: дальнейшую работу с репозиториями
   и обновление самого инструмента выполняйте командами GDI. Не повторяйте ручное
   восстановление bundles после появления работоспособной доверенной копии.

## Обновить локальную или mount-копию GDI агента с Google Drive

Просьба «обнови GDI» означает получить опубликованную версию утилиты из её
GDI-репозитория на Google Drive и использовать её вместо текущей копии агента.
Направление обмена: **Drive → локальный инструмент агента**. Такая просьба
не поручает разрабатывать GDI, создавать commits, публиковать локальные исходники
на Drive, запускать CI или обновлять worker на компьютере пользователя.
Рабочий репозиторий задачи (`WORK_DIR`) при этом не меняется.

До восстановления новой копии используйте **текущий доверенный GDI**:
`agent snapshot` определяет нужные bundles, `agent clone` проверяет их и создаёт
готовую новую копию. Агент скачивает указанные файлы коннектором, а не собирает
bundles или репозиторий вручную.

1. Определите текущий `INSTALL_DIR`, откуда агент запускает `python3 -m gdi`.
   Получите папку/Drive ID репозитория **самой утилиты GDI**, его доверенный
   Repository ID и нужную ветку из уже известных настроек или сообщения владельца.
   Это отдельные параметры: не подставляйте Repository ID, папку или ветку рабочего
   проекта. Если источник или ветка неизвестны, уточните только недостающие данные.
   Запишите текущий HEAD, если у локальной копии есть git metadata.

   ```bash
   GDI_CURRENT_DIR="$INSTALL_DIR"
   GDI_REPOSITORY_ID="доверенный_repository_id_репозитория_утилиты"
   GDI_BRANCH="ветка_утилиты_указанная_владельцем"
   GDI_UPDATE_DIR="/путь/к/обмену/для/этого/обновления"
   NEW_GDI_DIR="/путь/к/новой/копии/gdi"
   ```

   `GDI_UPDATE_DIR` должен находиться вне tracked файлов обоих репозиториев,
   а `NEW_GDI_DIR` — быть доступным для записи и ещё не существовать.
   `REPOSITORY_ID` и `WORK_DIR` рабочего проекта сохраните без изменений.

2. Коннектором скачайте свежие `repository.json` и все manifests выбранной ветки
   репозитория утилиты. Получите полные listings его корня, `branches` и каталога
   ветки. Соберите `$GDI_UPDATE_DIR/source/snapshot.json` по разделу
   «Снимок коннектора» ниже, с `ref: refs/heads/<GDI_BRANCH>` и реальными Drive IDs
   именно этого репозитория. Текущая доверенная копия GDI проверяет снимок:

   ```bash
   cd "$INSTALL_DIR"
   python3 -m gdi agent snapshot \
     --snapshot "$GDI_UPDATE_DIR/source/snapshot.json" \
     --repository-id "$GDI_REPOSITORY_ID" > "$GDI_UPDATE_DIR/source-plan.json"
   ```

   Продолжайте только после успешной команды. Из ответа сохраните `head`, `ref`,
   `publication_id` и `required_bundles`. Цель обновления — опубликованный tip
   указанной ветки. Если текущий HEAD уже равен этому `head` и worktree чист,
   сообщите, что копия актуальна. Номер версии пакета может совпадать у разных
   commits; сравнивайте SHA. Локальные изменения сохраните в прежней копии,
   а опубликованную версию восстановите отдельно.

3. Скачайте коннектором bundles из `required_bundles`, добавьте полный listing
   `bundles` и downloads в тот же снимок. Сохраните точные bytes metadata и bundles.
   Не выбирайте один произвольный bundle: GDI определяет checkpoint и нужные дельты.
   Восстановите опубликованную версию в отдельный каталог текущей копией инструмента:

   ```bash
   python3 -m gdi agent clone \
     --snapshot "$GDI_UPDATE_DIR/source/snapshot.json" \
     --repository-id "$GDI_REPOSITORY_ID" --destination "$NEW_GDI_DIR" \
     > "$GDI_UPDATE_DIR/clone-result.json"
   git -C "$NEW_GDI_DIR" rev-parse HEAD
   git -C "$NEW_GDI_DIR" symbolic-ref --short HEAD
   ```

   Продолжайте только после успешного clone. Сверьте его HEAD/ветку и ответ clone
   с `head`/`ref`/`publication_id` проверенного снимка. Clone проверяет repository ID,
   SHA256, цепочку публикаций и git objects до создания готового каталога. Если
   известен прежний HEAD, проверьте в новом clone, что он является предком целевого
   HEAD (`git merge-base --is-ancestor`). При расхождении или отсутствии старого
   commit сохраните обе копии и уточните источник/ветку перед переключением.
   Если старая копия не понимает опубликованный протокол, используйте процедуру
   «Первое получение самих исходников GDI», а не обходите проверки.

4. Прочитайте инструкции и обновлённый `docs/agent.md` из нового clone. Переключите
   путь запуска агента на проверенную копию:

   ```bash
   INSTALL_DIR="$NEW_GDI_DIR"
   cd "$INSTALL_DIR"
   python3 -m gdi --version
   python3 -m gdi agent --help
   python3 -c 'import gdi; print(gdi.__file__)'
   ```

   Путь `gdi.__file__` должен указывать на новую копию. Используйте этот `INSTALL_DIR`
   во всех следующих вызовах; запуск по прежнему mount-пути оставит старый код.
   Для read-only mount достаточно переключить путь на новый доступный clone:
   не пытайтесь перезаписать или удалить точку монтирования. Старую копию сохраните.

   `agent clone` требует отсутствующий каталог назначения и не обновляет существующий
   mount-путь на месте. Используйте восстановленный GDI и переключение `INSTALL_DIR`,
   а не ручной импорт bundles или принудительную замену файлов старой копии.

5. Сообщите источник на Drive, Repository ID утилиты, ветку, прежний HEAD (если
   известен), полученный HEAD/Publication ID, фактический `INSTALL_DIR` и результат
   проверки запуска. Обновление завершено, когда следующие команды агента используют
   скачанную версию. Получение нового clone без переключения используемого пути
   не означает, что инструмент агента обновлён.

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

Перед подготовкой определите фактический `worker_id` и общий Drive root по worker
config доступного CI host или сведениям его владельца. Сохраните ID в переменной
`WORKER_ID`; это ID исполнителя, независимый от имени рабочего репозитория.
Имена вроде `user-host`
в примерах других документов требуют замены фактическим ID. GDI использует явно
переданный `--worker` и автоматически работающего исполнителя не выбирает.

В общем root могут оставаться каталоги прежних workers. Наличие
`ci/workers/WORKER_ID/capabilities.json` само по себе не подтверждает, что этот worker
сейчас запущен. Для диагностики скачайте его свежий `status.json`: старый `RUNNING`
с неизменным `updated_at` не доказывает текущее исполнение. Статус `READY` также
не заменяет проверку службы на host. Если нужный ID неизвестен или сведения
противоречат друг другу, уточните его у владельца host перед отправкой запроса.

```bash
WORKER_ID="фактический_worker_id_из_config_CI_host"
```

Скачайте свежие `ci/workers/$WORKER_ID/capabilities.json` из **общего root**, получите
свежий снимок проекта с проверенной публикацией. Workflows должны присутствовать
в точном опубликованном commit; локальная ветка может быть уже впереди него.
Сохраните скачанный файл как `$EXCHANGE_DIR/capabilities.json`; поле `worker_id`
в нём должно совпадать с `$WORKER_ID`. Используйте capabilities этого же worker
при prepare, check и accept. Revision выбранного профиля GDI берёт из этого файла;
скачивание неизменившегося файла прежнего worker не делает его окружение актуальным.

```bash
python3 -m gdi agent ci prepare --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/published/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --publication PUBLICATION_ID --worker "$WORKER_ID" --profile full \
  --capabilities "$EXCHANGE_DIR/capabilities.json" \
  --output "$EXCHANGE_DIR/ci-plan"
```

Поддерживаются те же `--workflow`, `--event`, `--job`, повторяемый `--input KEY=VALUE`.
План содержит `files.request`, `files.ready`, `files.inbox` и фиксированные job ID,
profile revision, ref/HEAD и Publication ID. Порядок: **request → ready → inbox**.
Повтор prepare с тем же планом сохраняет job ID/bytes. Изменение capabilities
требует нового явного запроса; при простой задержке worker новый job не создавайте.
Другой worker пропускает адресованное чужому ID событие inbox. Если выбран неверный
ID или изменилась revision, сохраните прежний план и запрос, затем выполните новый
prepare в отдельном каталоге с правильным ID и свежими capabilities. Не меняйте
worker ID или revision вручную в уже опубликованных immutable файлах. Перезапуск
worker с другим ID не перенаправляет существующее задание.

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

`safe_to_upload_inbox: true` подтверждает проверку запроса перед загрузкой inbox,
а `state: accepted` — проверку его передачи в inbox. Эти ответы не означают, что
worker уже получил задание или начал CI. Проверяйте дальнейший прогресс именно
у worker с ID из плана и у соответствующего job.

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
