# GDI через коннектор Google Drive

Агент запускает GDI из полученных исходников или готового `gdi.pyz`.
Нужны Python 3.10+ на Linux и Git;
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

В среде агента ChatGPT/OAI живые Git checkout-ы, полученные через GDI, храните
под `/home/oai/share/gdi-checkouts/<repo-name>`. Например:
`/home/oai/share/gdi-checkouts/serialterminal` или
`/home/oai/share/gdi-checkouts/gdi`. Не используйте `/mnt/data` как постоянный
`WORK_DIR` или `INSTALL_DIR`: этот каталог предназначен для временных downloads,
snapshots, bundles, manifests и connector materialization; runtime может повторно
материализовать туда файлы и заменить содержимое существующего каталога. После
`agent clone` проверьте наличие `.git`, фактические HEAD/ветку и чистоту worktree.
Каталог `/home/oai/share/gdi-checkouts` остаётся рабочей копией текущей execution
environment, а опубликованный GDI-репозиторий на Drive — источником истины; при
потере среды восстановите checkout заново через `gdi agent`.

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

### Готовый runtime gdi.pyz

Если локальная среда очищена и рабочего Python-пакета GDI больше нет, сначала
попробуйте получить готовый runtime. В репозитории самой утилиты на Drive он лежит
в `runtime/gdi.pyz`: для текущей установки владельца это
`gdrive:gdi/gdi/runtime/gdi.pyz` (папка Google Drive `gdi/gdi/runtime`). Rclone-путь
служит указанием расположения; агент скачивает файл через коннектор Drive,
без rclone, распаковки, pip, `.git` или настройки `PYTHONPATH`.

1. Получите доверенный SHA256 от владельца либо из ранее доверенных сведений
   о выпуске. Не назначайте hash вычисленного скачанного файла доверенным.
   `runtime.json` и `gdi.pyz.sha256` рядом с архивом описывают выпуск и помогают
   проверить согласованность скачивания, но сами по себе не являются независимым
   источником доверия. Обновление файлов не атомарно; при несовпадении повторите
   скачивание и не запускайте архив.
2. Скачайте архив и сопровождающий `gdi.pyz.sha256` коннектором в
   `/mnt/data/gdi.pyz` и `/mnt/data/gdi.pyz.sha256`. Если коннектор добавил к
   имени checksum-файла суффикс `.bin`, приведите локальное имя к указанному.
3. Проверьте SHA256 до исполнения:

   ```bash
   GDI_RUNTIME="/mnt/data/gdi.pyz"
   GDI_RUNTIME_SHA256="доверенный_sha256_от_владельца"
   printf '%s  %s\n' "$GDI_RUNTIME_SHA256" "$GDI_RUNTIME" | sha256sum --check --strict
   ```

4. После успешной проверки сохраните одинаковые копии архива и checksum-файла
   **в обоих каталогах: `/mnt/data` и `/home/oai`**. Эти каталоги могут очищаться
   независимо; в наблюдаемом случае `/mnt/data/gdi.pyz` сохранился после потери
   `/home/oai/gdi.pyz`. Проверяйте SHA256 скопированного архива перед исполнением:

   ```bash
   cp /mnt/data/gdi.pyz /home/oai/gdi.pyz
   cp /mnt/data/gdi.pyz.sha256 /home/oai/gdi.pyz.sha256
   printf '%s  %s\n' "$GDI_RUNTIME_SHA256" /home/oai/gdi.pyz | sha256sum --check --strict
   ```

   После каждого обновления runtime обновляйте обе копии и их checksum-файлы.
5. После успешной проверки запускайте:

   ```bash
   python3 "$GDI_RUNTIME" --version
   python3 "$GDI_RUNTIME" agent --help
   ```

В дальнейших примерах `python3 -m gdi` заменяйте на `python3 "$GDI_RUNTIME"`.
`agent snapshot` и `agent clone` восстанавливают проекты обычным проверенным
способом. Для изменения самой утилиты отдельно восстановите её исходники через
`agent clone`. Архив — готовый инструмент агента, а не checkout исходников.
Нужны Python 3.10+ и Git. Если одна копия исчезнет, проверьте оставшуюся по
доверенному SHA256, восстановите из неё вторую копию вместе с checksum-файлом
и задайте `GDI_RUNTIME` на существующий проверенный архив. Если исчезли обе,
скачайте и проверьте runtime заново. Две локальные копии не гарантируют сохранность
между средами; правило о checkout-ах вне `/mnt/data` остаётся в силе.

### Bootstrap из полного bundle

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
не означает согласия запускать CI. Полученное разрешение на цикл исправлений
и CI сохраняется: не запрашивайте его заново для каждой итерации. Worker, act и Docker находятся на компьютере
пользователя. Агент только готовит и передаёт immutable файлы.

**Указывать конкретный `worker_id` необязательно.** Есть два режима подготовки:

- **Автоматический выбор:** передайте полный свежий `--workers` snapshot без
  `--worker`. GDI сам выберет доступного совместимого исполнителя.
- **Ручное назначение:** передайте `--worker ID` и его `--capabilities`, если
  задание должно выполняться на конкретном host. Скрытого fallback нет.

В обоих режимах определите общий Drive root и получите свежий снимок проекта
с проверенной публикацией. Workflows должны присутствовать в точном опубликованном
commit; локальная ветка может быть уже впереди него. У каждого host общего root
должен быть отдельный worker_id; одновременно запускать два экземпляра с одним ID нельзя.

### Автоматический выбор — без `--worker`

Заранее узнавать ID исполнителя и задавать `WORKER_ID` не требуется. Получите через
Drive-коннектор полный свежий снимок общей `ci/workers`: listings всех worker-папок
и downloads их capabilities/status. Сохраните его как `$EXCHANGE_DIR/workers.json`;
точный формат и правила полноты описаны в [scheduling.md](scheduling.md#агент-через-drive-коннектор).

```bash
python3 -m gdi agent ci workers --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/published/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --workers "$EXCHANGE_DIR/workers.json"
python3 -m gdi agent ci prepare --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/published/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --publication PUBLICATION_ID --profile full \
  --workers "$EXCHANGE_DIR/workers.json" --output "$EXCHANGE_DIR/ci-plan"
```

`workers` показывает доступность и причины исключения кандидатов; `prepare` выбирает
исполнителя и возвращает его `worker_id` в плане. По умолчанию нужен heartbeat моложе
300 секунд; предпочтение получает свободный host, затем меньшая очередь, при равенстве
исполнители чередуются. Если подходящего worker нет, подготовка завершится ошибкой.
Для другого способа выбора или ограничений добавьте `--policy PATH` к обеим командам:
доступны round robin, предпочтительный host с fallback, `pinned_worker`, метки и platforms.
Для устройств и локальных ресурсов соблюдайте заданную привязку.

Выбранный host может отличаться от текущего компьютера владельца. После prepare
используйте ID из плана для наблюдения за заданием и capabilities именно этого worker
для check/accept. Скачанный файл capabilities выбранного host сохраните как
`$EXCHANGE_DIR/capabilities.json` для общего порядка передачи ниже. Автоматический
выбор при подготовке сам по себе не переносит уже отправленное задание по таймауту.

### Ручное назначение — с `--worker ID`

Только в этом режиме перед подготовкой определите фактический `worker_id` по worker
config нужного CI host или сведениям его владельца. Сохраните ID в переменной
`WORKER_ID`; это ID исполнителя, независимый от имени рабочего репозитория.
На CI host выполните `gdi worker check --json`: поле `worker_id` берётся из
`~/.config/gdi/worker.json`. Если config находится в другом месте, используйте
`gdi worker check --config PATH --json`. Состояние службы проверьте отдельной
командой `gdi worker status`; успешная проверка config не доказывает доступность.
Имена вроде `user-host` в примерах требуют замены фактическим ID. Явный `--worker`
адресует запрос только этому host, даже если другие workers доступны.

В общем root могут оставаться каталоги прежних workers. Наличие
`ci/workers/WORKER_ID/capabilities.json` само по себе не подтверждает, что этот worker
сейчас запущен. Для диагностики скачайте его свежий `status.json`: старый `RUNNING`
с неизменным `updated_at` не доказывает текущее исполнение. Статус `READY` также
не заменяет проверку службы на host. Если нужный ID неизвестен или сведения
противоречат друг другу, уточните его у владельца host перед отправкой запроса.

Если владелец сменил компьютер, заново определите выбранный CI host и его
`worker_id` до следующего запроса. Не переносите сохранённый `WORKER_ID` с прошлого
host автоматически: выключенный worker не заберёт inbox, а второй компьютер
пропускает запросы с чужим ID. Если оба host включены, используйте указанный владельцем
ID или перейдите к автоматическому выбору по согласованной политике.

```bash
WORKER_ID="фактический_worker_id_из_config_CI_host"
```

Скачайте свежие `ci/workers/$WORKER_ID/capabilities.json` из **общего root**.
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

### Передача и проверка — для обоих режимов

Поддерживаются те же `--workflow`, `--event`, `--job`, повторяемый `--input KEY=VALUE`.
План содержит `files.request`, `files.ready`, `files.inbox` и фиксированные job ID,
profile revision, ref/HEAD и Publication ID. Порядок: **request → ready → inbox**.
Повтор prepare с тем же планом сохраняет job ID/bytes. Изменение capabilities
требует нового явного запроса; при простой задержке worker новый job не создавайте.
Другой worker пропускает адресованное чужому ID событие inbox. Если выбран неверный
ID или изменилась revision, сохраните прежний план и запрос, затем выполните новый
prepare в отдельном каталоге: без `--worker` со свежим `--workers` snapshot либо
с нужным `--worker ID` и свежими capabilities. Если прежний запрос уже передан,
сначала выполните отмену и используйте `--retry-of` или автоматический supervisor;
новый самостоятельный запрос не отменяет прежний. Не меняйте
worker ID или revision вручную в уже опубликованных immutable файлах. Перезапуск
worker с другим ID не перенаправляет существующее задание.

Для отмены используйте `gdi agent ci cancel/cancel-check`, а для явной замены —
`gdi agent ci prepare --retry-of`, как описано ниже. Не имитируйте отмену удалением
или правкой request/ready/inbox/claim файлов на Drive: worker мог сохранить запрос
локально и выполнить его после включения. Автоматический перенос выполняйте
`gdi agent ci supervise` с заданной политикой; tools сами выбирают совместимого
исполнителя, готовят отмену, проверяют доставку и сохраняют новую попытку.
Порядок snapshots/передачи: [scheduling.md](scheduling.md).

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

## Отменить CI и явно назначить замену

Используйте этот порядок при разрешённой явной отмене или замене исполнителя;
для автоматической политики переноса используйте [supervisor](scheduling.md).
Действуйте по указанию пользователя; просьба только отменить job не означает
разрешения пересоздать его. Получите свежий полный снимок проекта с исходными
request/ready и скачайте capabilities исходного worker из общего root.
Его `cancel_version: 1` подтверждает поддержку отмены, но не доступность host.
Если результат уже опубликован, сначала скачайте полный результат и его файлы:
проверенный terminal result не отменяется задним числом.

```bash
python3 -m gdi agent ci cancel --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/before-cancel/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --job OLD_JOB_ID --capabilities "$EXCHANGE_DIR/old-worker-capabilities.json" \
  --output "$EXCHANGE_DIR/cancel-plan"
```

Если ответ содержит `already_finished: true, verified: true`, задание уже
завершено: сохраните проверенный результат, cancel-файл не загружайте.
После `safe_to_upload_cancel: true` передайте коннектором точный файл `files.cancel`
из плана по его `target_folder`/`target_name`, используя `local_path`.
Не собирайте cancel.json самостоятельно. При потере ответа переиспользуйте
этот же план и bytes; сначала проверьте существующий файл по полному listing.
Скачайте файл обратно и включите его в свежий полный снимок с исходным запросом:

```bash
python3 -m gdi agent ci cancel-check --repo "$WORK_DIR" \
  --plan "$EXCHANGE_DIR/cancel-plan" \
  --snapshot "$EXCHANGE_DIR/cancel-proof/snapshot.json" --repository-id "$REPOSITORY_ID"
```

`state: accepted, cancellation_requested: true, verified: false` подтверждает
доставку отмены на Drive. Подтверждение остановки — проверенный результат
`CANCELLED`; выключенный worker сможет отправить его только после возвращения.
Не удаляйте marker, исходный запрос или историю: локальная очередь worker
учитывает отмену даже после исчезновения inbox event.

Если замена разрешена, после проверки доставки отмены скачайте capabilities
нового совместимого worker. Укажите его фактический ID в `WORKER_ID`;
`old-worker-capabilities.json` остаётся файлом старого исполнителя, а
`capabilities.json` относится к новому. Для исходного стандартного запроса
с profile full и selector по умолчанию подготовьте отдельный план:

```bash
python3 -m gdi agent ci prepare --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/cancel-proof/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --publication PUBLICATION_ID --worker "$WORKER_ID" --profile full \
  --capabilities "$EXCHANGE_DIR/capabilities.json" --retry-of OLD_JOB_ID \
  --output "$EXCHANGE_DIR/replacement-plan"
```

Используйте исходные publication/profile/workflow: gdi проверяет их совпадение
и сохраняет связь `retry_of`, а также прежний GitHub namespace. Для нестандартного
запроса передайте исходный `--profile` и весь selector: `--workflow`, `--event`,
`--job`, повторяемые `--input`. Возьмите значения из сохранённого request.json;
selector по умолчанию не заменяет исходный. `PUBLICATION_ID` — публикация старого
job, а не новый tip ветки. Передайте новый запрос обычным порядком
prepare/check/accept. Retry разрешается по проверенному terminal result либо
по проверенной отмене поддерживающего её worker. Доставка отмены сама по себе
не доказывает, что процесс на недоступном host уже остановлен; учитывайте
возможность одновременного исполнения при выборе повторяемых заданий.
Отмена блокирует поздний результат также после сохранения локально/частичной
загрузки. Worker сохраняет исходные immutable файлы и подтверждает такую отмену
отдельным результатом в `ci/jobs/OLD_JOB_ID/cancelled/`. Перенос по таймауту
включается только отдельной supervisor-политикой повторяемости.
Для старого worker без поддержки отмены агентская команда откажет:
не подменяйте capabilities и не считайте удаление уведомления остановкой CI.

## Проверить результат CI

Сначала получите свежий полный listing `ci/jobs/JOB_ID`, исходные request/ready
и listings родительских папок. Дальнейшие downloads выбирайте по этому listing:

1. Если есть `cancel.json`, скачайте его и включите в снимок. Для старого
   `worker.running.json`, если он есть, также нужен download: gdi сверяет run_id
   подтверждения отмены с исходным claim.
2. Если есть `cancelled/`, получите полный listing этой папки. При наличии
   её result.json скачайте этот результат и все объявленные log/final status/
   artifacts, а также все `cancelled/log-chunks`. Listings всех родительских
   папок должны быть полными. Старый result и artifacts вне `cancelled/` остаются
   историей и для подтверждения отмены не требуются. Этот результат используется
   вместе с проверенным cancel marker; одно наличие папки не доказывает отмену.
3. Если отдельного результата отмены нет, используйте обычный result.json.
   Для подтверждённого в нём CANCELLED или обычного terminal result без отмены
   скачайте все объявленные artifacts, final status, полный build.log и **все**
   log chunks. Если есть поддерживаемая отмена, обычный поздний PASS/FAIL не
   является действующим результатом: gdi вернёт CANCEL_REQUESTED без скачивания
   его исторических artifacts. Снимок должен содержать metadata result.json,
   если этот файл присутствует в listing и отдельного результата отмены ещё нет.

Пустой лог может не иметь папки log-chunks; присутствующая папка требует полного
listing даже при отсутствии chunks. Отсутствие result в полном listing означает
ожидание; отсутствие нужного listing/download в снимке — ошибка неполного снимка.
Events и mutable status не подтверждают PASS. До подтверждения остановки worker
команда вернёт `CANCEL_REQUESTED, verified:false`, даже если на Drive успел попасть
старый PASS. Исключение — withdrawal старого worker с
`worker_cancellation_supported:false`: он не доказывает остановку и не блокирует
обычный проверенный результат, как описано в [ci-protocol.md](ci-protocol.md).
Обновите gdi у агента и worker перед использованием отмены; прежние клиенты
могут не учитывать cancel marker при проверке результатов.

```bash
python3 -m gdi agent ci result --repo "$WORK_DIR" --plan "$EXCHANGE_DIR/ci-plan" \
  --snapshot "$EXCHANGE_DIR/result/snapshot.json" --repository-id "$REPOSITORY_ID"
```

Используется тот же проверяющий код, что у обычного CI-клиента: identity запроса,
результата и final status, SHA256/размер каждого artifact, непрерывность chunk sequence,
совпадение chunks с полным binary log и успешные blocking stages для PASS.
`PENDING, verified:false` означает, что result ещё отсутствует в полном listing.
Скачанные artifacts проверенного результата сохраняются в каталоге плана.

| Ответ | Действие агента |
| --- | --- |
| `PENDING, verified:false` | Проверять существующий job и его worker; не создавать дубликат из-за задержки |
| `CANCEL_REQUESTED, verified:false` | Отмена доставлена, остановка пока не подтверждена; разрешённую замену готовить через --retry-of |
| `CANCELLED, verified:true` | Остановка подтверждена; сохранить историю и завершить отмену либо проверить уже созданную замену |
| `PASS, verified:true` | Принять только точные job/head/publication/profile/revision проверенного результата |
| Другой terminal state с `verified:true` | Читать detail и лог; повтор исполнения только по согласованной задаче |
| `kind:GDI_ERROR` | Исправить снимок/передачу по сообщению gdi; не объявлять CI завершённым по advisory status |

Для явного retry используйте новый каталог плана и `gdi agent ci prepare
--retry-of OLD_JOB_ID`. Основание — полный проверенный terminal result либо
доставленная и проверенная отмена поддерживающего её worker. Активное задание
без такого основания повторять нельзя. Повтор передачи прежнего плана —
повтор доставки того же job, а не новое исполнение. Если требуется проверить
другой commit/publication, создайте самостоятельный запрос по разрешённому
CI-циклу: это не retry старого commit и не его отмена.

## Наблюдение за CI

После передачи своего CI-запроса через `agent ci accept` наблюдайте за **этим
заданием до проверенного terminal result**. `accepted` подтверждает передачу
запроса, но не начало и не завершение сборки. Наблюдение выполняется через
Drive-коннектор и локальные snapshots: агенту не нужны rclone, SSH, journalctl
или доступ к файловой системе worker. Все действия этого раздела — чтение.

### 1. Определить свою задачу по сохранённому плану

Используйте каталог плана, которым отправили запрос, даже если после этого
локальный HEAD изменился. Сохранённые `job_id`, `worker_id`, `head`, `ref` и
`publication_id` фиксируют задачу; имена компьютеров и последний job в общей
папке не заменяют эти значения. Это относится и к автоматическому выбору host:
нужный `worker_id` уже записан GDI в плане.

```bash
CI_PLAN="$EXCHANGE_DIR/ci-plan"
python3 - "$CI_PLAN" <<'PY'
import json
from pathlib import Path
import sys

plan = json.loads((Path(sys.argv[1]) / 'plan.json').read_text())
print(json.dumps({key: plan[key] for key in
                 ('repository_path', 'repository_id', 'job_id', 'worker_id',
                  'ref', 'head', 'publication_id')}, ensure_ascii=False, indent=2))
print('Папка задачи в общем Drive root:', plan['files']['request']['target_folder'])
PY
```

Это только просмотр локального плана; его согласованность с request и снимком
проверяет `agent ci result`. Доверенный `$REPOSITORY_ID` по-прежнему берите из
согласованных сведений пользователя, как в разделе «Снимок коннектора».
Не переписывайте план/request и не вызывайте prepare заново ради наблюдения.
При разрешённом переносе supervisor создаёт новую попытку: используйте активный
job и план из его сохранённой сессии, сохраняя историю прежнего job. Порядок
перехода описан в [scheduling.md](scheduling.md#агент-через-drive-коннектор).

### 2. Найти heartbeat задачи на Google Drive

Heartbeat не отдельный файл `heartbeat.json`: он находится в **mutable
`status.json` конкретного job**. Путь относительно папки GDI-репозитория:

```text
ci/jobs/JOB_ID/status.json
```

Папку задачи определяйте по `files.request.target_folder` сохранённого плана.
Этот путь относителен общему Drive root; `status.json` лежит рядом с переданными
`request.json` и `request.ready`. Например, для репозитория самой утилиты в
текущей установке владельца папка Drive — `gdi/gdi`, а файл задачи —
`gdi/gdi/ci/jobs/JOB_ID/status.json`. Это пример расположения, не обязательное
имя для других репозиториев.

Общий статус host расположен иначе: `ci/workers/WORKER_ID/status.json`
**относительно общего root**, например `gdi/ci/workers/user-dream/status.json`.
Он полезен для диагностики доступности host, но не заменяет heartbeat своей
задачи: worker может уже выполнять другой job. Для существующего CI используйте
job ID из плана, а не только статус worker или наличие его capabilities.

### 3. Получить свежий snapshot через Drive-коннектор

На каждом цикле наблюдения создавайте новый локальный каталог, например
`$EXCHANGE_DIR/heartbeat-001`, затем `heartbeat-002`. Не считайте старый snapshot
живым подключением к Drive: локальная команда сама ничего с Drive не читает.

1. Коннектором получите свежие **полные listings всех страниц** папки проекта,
   `branches`, ветки из плана, `ci`, `ci/jobs` и `ci/jobs/JOB_ID`. Для перехода
   к дочерней папке используйте её Drive folder ID из listing родителя. Не
   заменяйте listing папки результатом поиска одного `status.json`; так нельзя
   подтвердить отсутствие результата и обнаружить дубликаты имён.
2. Скачайте свежий `repository.json` и manifests выбранной ветки, необходимые
   снимку. Неизменившиеся immutable manifests/request/ready можно скопировать
   из прежнего проверенного снимка в новый каталог по правилам раздела
   «Снимок коннектора». Git bundles для одного наблюдения за CI не нужны.
   Снимок должен содержать исходные request/ready именно своего job.
3. Если в listing задачи есть `status.json`, скачайте его коннектором **по
   найденному file ID** в новый локальный файл, даже если имя и ID не изменились.
   Это mutable файл: прежний download нельзя переиспользовать как свежий.
   Сохраните исходные bytes; просмотр JSON не должен перезаписывать файл.
   Скачивайте и читайте status на каждом цикле, а не только при первом появлении
   файла. `modified_time` из metadata Drive не заменяет содержимое status и его
   `updated_at`: файл мог быть опубликован позднее обновления локального статуса
   worker. Отмечайте отдельно timestamp worker и время, когда агент фактически
   прочитал эти bytes. Так можно отличить задержку публикации от позднего poll.
4. Соберите `snapshot.json` формата версии 1 из перечисленных listings и
   downloads. Укажите ту же папку проекта, `repository_path` и `ref`, что у
   своего плана. В `files` обязательно добавьте скачанный status с реальным
   Drive file ID и путём относительно нового snapshot. Например, **элемент
   массива `files`**, а не самостоятельный snapshot:

   ```json
   {"file_id": "STATUS_FILE_ID", "local_path": "downloads/status.json"}
   ```

   В listing папки job этот же ID должен соответствовать имени `status.json`,
   `is_dir: false` и фактическому `bytes` из metadata коннектора. Сохраняйте весь
   listing, включая request/ready и другие файлы. Абсолютные local_path, `..`
   и symlinks не поддерживаются. Если status изменился между listing и download
   и размер не совпал, обновите listing/download; не исправляйте размер вручную
   и не подставляйте старые bytes.
5. По свежему listing проверьте наличие `cancel.json`, `cancelled/` и
   `result.json`. Если появилась отмена или результат, дополните снимок по
   разделу «Проверить результат CI»: нужны соответствующие downloads, полные
   listings дочерних папок, final status, artifacts, полный build.log и все
   chunks. Одного status недостаточно для проверки завершённой задачи.

После появления terminal result сначала скачайте соответствующие `result.json`,
`final-status.json` и `build.log`, чтобы сообщить заявленный системой исход и
прочитать причину падения, затем соберите остальные обязательные artifacts/chunks.
При отмене выбирайте действующий результат по разделу «Проверить результат CI»;
исторический обычный result не заменяет результат отмены. Предварительное чтение
этих файлов не даёт `verified:true`, но позволяет сообщить пользователю о FAIL
и найденной в логе ошибке до окончания скачивания всех chunks.

Если status ещё нет в **полном** listing, не добавляйте выдуманный файл или
download: GDI покажет `heartbeat.state: missing`. Это допустимо, например,
до начала обработки запроса. Если коннектор не смог прочитать папку/скачать
файл, сообщите сетевую ошибку и повторите чтение позже; ошибка не доказывает
отсутствие heartbeat, завершение или отмену CI.

### 4. Проверить heartbeat через GDI и прочитать текущий этап

```bash
HEARTBEAT_SNAPSHOT="$EXCHANGE_DIR/heartbeat-001/snapshot.json"
python3 -m gdi agent ci result --repo "$WORK_DIR" --plan "$CI_PLAN" \
  --snapshot "$HEARTBEAT_SNAPSHOT" --repository-id "$REPOSITORY_ID" \
  --heartbeat-timeout 600 > "$EXCHANGE_DIR/heartbeat-001/ci-observation.json"
cat "$EXCHANGE_DIR/heartbeat-001/ci-observation.json"
```

Для готового runtime используйте `python3 "$GDI_RUNTIME" agent ci result` с
теми же аргументами. Команда проверяет identity проекта, публикации, плана,
request/ready и соответствие скачанного status своей задаче. Не объявляйте
наблюдение успешным при `kind: GDI_ERROR`: исправьте указанный снимок/передачу,
не подменяя проверки ручным разбором JSON.

Сохраняйте фактическую команду, её код завершения и JSON-ответ для каждого
проверенного snapshot. `agent ci result --help`, чтение реализации `result()`
и сообщение «сейчас прогоню проверку» не являются выполненной проверкой.
После подготовки полноценного снимка реально запустите команду с `--plan`,
`--snapshot`, `--repo` и доверенным `--repository-id`. Не объявляйте проверенный
итог, пока её ответ не содержит соответствующие `state` и `verified:true`.

Пример ответа для незавершённого CI:

```json
{
  "job_id": "264eab9c535a49338faadc3c6eb5f05e",
  "state": "PENDING",
  "verified": false,
  "heartbeat": {
    "updated_at": "2026-10-14T13:49:50.120594+00:00",
    "age_seconds": 12.18,
    "timeout_seconds": 600,
    "state": "fresh"
  }
}
```

Дата и ID здесь иллюстративны, не используйте их для новой задачи. Внешний
`state: PENDING` означает отсутствие проверенного terminal result; это не
утверждение, что worker ещё не начал сборку. Его текущие `state` (`RESTORING`,
`CHECKOUT`, `RUNNING` и т. п.), `stage`, `sequence` и `run_id` смотрите в
скачанном `downloads/status.json`. `agent ci result` при PENDING возвращает
heartbeat, но не копирует все эти поля status в свой ответ.

`updated_at` — время обновления статуса worker, а не время скачивания агентом.
GDI вычисляет возраст относительно часов среды агента. Timestamp содержит
часовой пояс; `+00:00`/`Z` означает UTC, для отображения в Москве прибавьте
3 часа, но не меняйте скачанные bytes. Повторное скачивание прежнего статуса
не делает его свежим. `sequence` и timestamp могут продвигаться при том же
`stage`: долгий тест или загрузка остаются одним этапом.

| `heartbeat.state` | Что это означает и что делать |
| --- | --- |
| `fresh` | Возраст меньше 600 секунд при выбранном пороге. Worker недавно обновил опубликованный статус; продолжайте наблюдение. Это не доказательство PASS или продвижения отдельного теста. |
| `stale` | Возраст достиг порога. Обновите снимок, проверьте сеть/доставку и статус назначенного host; это само по себе не доказывает остановку worker или FAIL. |
| `missing` | Нет status/timestamp в доступных данных. Задача могла ещё не начаться; проверьте полный listing и сохранённый job ID, затем продолжайте ожидание. |
| `unavailable`, `reason_code: status_not_downloaded` | Status есть в listing, но его bytes не включены в снимок. Скачайте файл коннектором, добавьте его в `files` нового snapshot и повторите команду. |
| `invalid` | Timestamp некорректен. Повторите download; если проблема сохраняется, сообщите диагностику, не исправляя опубликованный JSON вручную. |
| `clock_skew` | Timestamp более чем на 30 секунд впереди часов агента. Проверьте часы host/агента; отрицательный возраст не означает свежий CI. |

Порог по умолчанию — 600 секунд; `--heartbeat-timeout SECONDS` меняет только
диагностику этого вызова и не отменяет CI. Даже `state: PASS` или `FAIL` в
mutable status ещё не заменяет проверенный result: выполнение могло закончиться,
а worker ещё загружает artifacts/result на Drive. Перейдите к проверке результата
по мере его появления и объявляйте итог только после `verified:true`. Код 0
`agent ci result` сам по себе не означает PASS.

При первом прочитанном `state: FAIL` **сразу сообщите пользователю о падении,
не ждите следующего цикла или доставки всех artifacts ради этого сообщения**:
«worker сообщил FAIL; проверенный результат и полный лог ещё передаются».
Больше не ожидайте успешного завершения этой попытки. Если нужно выяснить причину
и получить подтверждённый итог, продолжайте только получение result/log/artifacts
и называйте это ожиданием доставки. Если они долго не появляются, сообщите
последний status, его время, отсутствующие файлы и ошибки коннектора вместо
бесконечного «жду сборку». При `PASS` также отделяйте сообщение worker от
проверенного `PASS, verified:true`.

### 5. Повторять наблюдение и сообщать фактический прогресс

При циклической проверке через коннектор выдерживайте паузу не меньше 30 секунд;
в простое и после сетевой ошибки используйте 120 секунд. Worker по умолчанию
отдельно опрашивает inbox с паузой 30–120 секунд; локальный heartbeat раз в
5 секунд не обращается к Drive. На Drive статус поступает с интервалом публикации
и задержкой сетевых операций; ожидать новый timestamp каждые 5 секунд не нужно.
Таймаут ожидания/исполнения 3600 секунд не задаёт интервал проверки и не запускает
автоматическую отмену. Причины недоступности host в `agent ci workers` описаны в
[scheduling.md](scheduling.md).

Один цикл: **пауза → свежие listings/downloads → новый snapshot →
`agent ci result` → сообщение о наблюдаемом состоянии**. Не ограничивайтесь
повторением listing `ci/jobs/JOB_ID` и фразой «жду сборку»: listing сообщает
наличие файла, но не свежесть heartbeat и не текущий этап.

Сообщайте job/worker ID, HEAD, состояние и stage из status, время его обновления,
возраст/свежесть heartbeat и что именно ожидается. Например: «job 264e… на
user-dream проверяет f380…; RUNNING/github-actions, heartbeat fresh, возраст
12 секунд; result.json ещё не опубликован». Если status уже FAIL/PASS, а result
ещё нет, сообщайте: «worker сообщил завершение исполнения; ожидаю доставку и
проверку результата», а не «CI всё ещё исполняется».

Различайте три фазы в сообщениях пользователю:

| Фаза | Что сообщать и делать |
| --- | --- |
| Worker выполняет CI | Текущие state/stage, timestamp, возраст heartbeat; продолжать свежие polls. |
| Worker уже сообщил FAIL/PASS, обязательные файлы ещё не получены | Исход уже сообщён; назвать конкретное ожидание: публикация result, download лога или сбор оставшихся artifacts/chunks. Не называть это ожиданием окончания сборки. |
| Полный snapshot готов | Выполнить настоящий `agent ci result`, затем сообщить state/verified, HEAD, failed_stage/exit_code и причину из лога. Одного наличия файлов на Drive недостаточно. |

При получении многих chunks сообщайте фактический прогресс: например,
«система сообщила FAIL; result и build.log скачаны, получено 8 из 26 chunks;
проверка GDI ещё не выполнена». Числа берите из полного listing и реально
полученных файлов, не из этого примера. Если коннектор поддерживает пакетные
или параллельные независимые downloads, используйте их после получения полных
listings; не добавляйте 30-секундные паузы между скачиванием уже опубликованных
immutable chunks. Интервал polling относится к ожиданию новых данных.
Неизменившиеся проверенные downloads можно переиспользовать по прежним правилам,
но пропускать обязательные chunks ради ускорения `verified:true` нельзя.
Если пользователь остановил сбор файлов, сообщите состояние системы и перечень
оставшихся файлов, явно указав, что формальная проверка GDI не завершена.

Свежий heartbeat при неизменном логе не требует нового запроса или переноса.
Устаревший heartbeat, пауза публикации и исчезновение inbox тоже не разрешают
создавать дубликат CI: повтор/перенос выполняйте только по согласованной политике
через существующие cancel/retry/supervise-команды, сохраняя историю задач.

Не объявляйте фоновый мониторинг установленным, если не создали реальную задачу
в доступном инструменте. Если внешний инструмент ограничивает частоту проверок,
укажите этот инструмент и его фактическое ограничение. В gdi нет ограничения
«проверять не чаще раза в час». При отсутствии фонового механизма сообщите
проверенное текущее состояние; обещание уведомить позднее не заменяет мониторинг.
