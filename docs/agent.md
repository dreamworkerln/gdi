# GDI через коннектор Google Drive

Агент получает и обновляет готовый `gdi.pyz` из папки `runtime/` на Drive.
Нужны Python 3.10+ на Linux и Git;
сторонние Python-пакеты, pip, setuptools, rclone, Docker и worker агенту не нужны.
GDI читает локальные снимки и готовит файлы. Все операции Drive выполняет коннектор.
Форматы Git protocol v3 и CI request v2 остаются общими с обычным клиентом.
Для CI через обычный CLI с rclone используйте [ci.md](ci.md).

## Границы задачи и использование инструмента

Работайте с репозиторием, веткой и задачей, указанными пользователем. GDI —
инструмент получения исходников, обмена и CI; его использование не включает
ревью самого GDI, коннектора Drive, контейнера агента, ОС или прочей инфраструктуры.
Роль независимого ревьюера и требование «не считать зелёный CI доказательством
корректности» относятся к проверяемой реализации в целевом репозитории и качеству
её тестов. Они не расширяют область ревью на используемые инструменты.
Сам GDI становится объектом ревью только по отдельной явной задаче пользователя.

Выполняйте штатные проверки командами GDI и используйте их проверенные результаты.
Не дублируйте вручную проверки hashes, manifests, цепочек публикаций или результатов
CI, уже выполненные GDI, и не запускайте повторные проверки только из общего
недоверия к инструменту. Это не отменяет обязательных шагов протокола, проверок
runtime и получения файлов для `verified:true`, описанных ниже. Изучение кода
целевого проекта, его тестов и CI-лога остаётся частью ревью этого проекта.

Если при работе GDI или среды обнаружена аномалия, сообщите пользователю исходную
ошибку или противоречие, шаг, на котором оно возникло, и влияние на текущую задачу.
Не превращайте это в самостоятельный аудит или исправление GDI, коннекторов,
контейнера или ОС. Допустимы предусмотренные этой инструкцией действия для
продолжения операции: исправление входного снимка, получение недостающего файла,
повтор доставки сохранённого плана. Если они не помогают, сохраните имеющиеся
данные, обозначьте препятствие и продолжайте только независимую часть исходной
задачи. Исследование и исправление инструмента требуют отдельной задачи.

В примерах команды запускают готовый runtime по абсолютному пути из любого
каталога. `--repo` задаёт рабочий Git-репозиторий проекта. Вывод команд — один JSON
на stdout; сообщения о ходе работы идут на stderr. Ошибка возвращает код 1 и JSON
с `kind: GDI_ERROR`. Обычное `gdi agent ci result` возвращает код 0 и для проверенного
FAIL: состояние CI определяется полями `state` и `verified`, а не кодом этой команды.

```bash
GDI_RUNTIME="/mnt/data/gdi.pyz"
WORK_DIR="/mnt/data/gdi-checkouts/my-project"
EXCHANGE_DIR="/mnt/data/gdi-exchange/my-project"
REPOSITORY_ID="доверенный_repository_id_пользователя"
python3 "$GDI_RUNTIME" agent --help
```

По наблюдению владельца, в используемой среде агента ChatGPT/OAI `/mnt/data`
сохраняется дольше, чем `/home/oai/share`. При этом **материализация может стереть
содержимое по обоим путям**: ни один из них не защищает рабочую копию от потери.
Для Git checkout-ов можно использовать отдельные каталоги под
`/mnt/data/gdi-checkouts/<repo-name>`, например
`/mnt/data/gdi-checkouts/my-project`.
`WORK_DIR` и `GDI_RUNTIME` могут находиться в `/mnt/data`; downloads, snapshots,
bundles, manifests и планы обмена храните отдельно, например в `/mnt/data/gdi-exchange`.
После `agent clone` проверьте наличие `.git`, фактические HEAD/ветку и чистоту
worktree. После материализации или восстановления среды проверьте сохранившийся
checkout перед продолжением работы: наличие `.git`, HEAD/ветку и состояние worktree.
Разделение каталогов checkout и обмена не гарантирует защиту от материализации.
Локальные каталоги не гарантируют сохранность между средами,
а опубликованный GDI-репозиторий на Drive — источник восстановления; при потере
checkout восстановите его заново через `gdi agent`.

Храните снимки и планы вне tracked файлов проекта. Для каждой новой передачи
выбирайте отдельный каталог плана. До завершения передачи сохраняйте этот каталог:
повтор использует прежние bytes, nonce и job ID. После ошибки не создавайте новый
план только из-за потери ответа коннектора. При загрузке сначала ищите точное имя
в полном listing: существующий файл с теми же bytes переиспользуется, отличающиеся
bytes или дубликаты имён требуют остановки. Не перезаписывайте immutable файлы.

## Обязательный способ работы с GDI-репозиториями

Для любого GDI-репозитория используйте существующие команды GDI. Это правило
распространяется и на исходники самой утилиты, если задача требует разработки GDI.
Получение и обновление исполняемого инструмента выполняется из папки runtime/
по следующему разделу. Не заменяйте обмен проектами собственными скриптами
или ручным воспроизведением протокола.

- Проверка снимка, выбор bundles и восстановление: `gdi agent snapshot`
  и `gdi agent clone`.
- Публикация: `gdi agent prepare`, `gdi agent check`, `gdi agent accept`.
- CI: `gdi agent ci prepare`, `accept`, `result`.

Коннектор выполняет listings, скачивание и загрузку файлов; агент собирает описанный
ниже снимок из этих listings и downloads и передаёт его GDI. Скачивайте bundles
по `required_bundles`, загружайте файлы по проверенному плану и соблюдайте решения
GDI о следующих шагах. Проверку identity, hashes, manifests, зависимостей bundles,
git objects и результатов CI выполняет GDI.

Не выбирайте checkpoint и дельты вручную, не собирайте из них репозиторий командами
`git bundle create/unbundle`, `git clone <bundle>` или `git fetch <bundle>`.
Не создавайте и не исправляйте вручную bundles, manifests, Publication IDs,
CI submission/result. Не пишите собственную реализацию их проверок вместо
команд GDI. Обычные git-команды для работы с исходниками и просмотра локального
HEAD/ветки разрешены; восстановление из файлов GDI-протокола делает `agent clone`.
При ошибке GDI исправьте указанные входные данные или получите свежий снимок;
если продолжение невозможно, сообщите причину, сохранив файлы и план.

## Получить или обновить GDI агента

### Готовый runtime gdi.pyz

Для первого запуска, после потери контейнера и для обычного обновления используйте
**готовый runtime из папки runtime/ владельца на Google Drive**. Для текущей
установки это gdrive:gdi/gdi/runtime/ (папка Drive gdi/gdi/runtime). Rclone-путь
здесь только обозначает расположение: все скачивания выполняет Drive-коннектор.
Исполняемая копия исходников GDI для этой процедуры не нужна.

Просьба «обнови GDI» означает скачать опубликованный runtime и переключить на него
следующие вызовы инструмента. Рабочий репозиторий проекта при этом не меняется.
Исходники самой утилиты восстанавливайте только по отдельной задаче на работу
с кодом GDI. Для обновления инструмента не собирайте Git snapshot, не скачивайте
manifests/bundles и не выполняйте clone репозитория самой утилиты.

1. Используйте папку runtime/, указанную владельцем или уже сохранённую в настройках.
   Это доверенный источник выпуска. Получите её свежий полный listing и скачайте
   по найденным file IDs **четыре файла**: gdi.pyz, gdi.pyz.sha256, runtime.json
   и agent.md. Используйте новый локальный каталог вне рабочего проекта, например:

   ```bash
   GDI_UPDATE_DIR="/mnt/data/gdi-runtime-update-001"
   mkdir -p "$GDI_UPDATE_DIR"
   ```

   Скачанные файлы сохраняйте туда с исходными именами. Если коннектор добавил
   checksum-файлу суффикс .bin, верните имя gdi.pyz.sha256. Не используйте дату
   изменения или номер версии вместо содержимого файлов.

2. Проверьте checksum, размер и согласованность скачанного выпуска. Для штатного
   обновления ожидаемые значения берутся из свежих runtime.json и gdi.pyz.sha256
   той же доверенной папки. Если владелец дал ожидаемый SHA256 именно этого выпуска,
   дополнительно сверяйте с ним; отдельно запрашивать hash при каждом обновлении
   из уже указанного доверенного источника не требуется.

   ```bash
   set -eu
   cd "$GDI_UPDATE_DIR"
   sha256sum --check --strict gdi.pyz.sha256
   python3 - <<'PY_RUNTIME'
   import hashlib
   import json
   from pathlib import Path

   metadata = json.loads(Path("runtime.json").read_text())
   runtime = Path("gdi.pyz").read_bytes()
   if len(runtime) != metadata["bytes"] or hashlib.sha256(runtime).hexdigest() != metadata["sha256"]:
       raise SystemExit("runtime.json does not match gdi.pyz; download the release again")
   print("Runtime HEAD:", metadata["head"])
   print("Runtime SHA256:", metadata["sha256"])
   PY_RUNTIME
   ```

   Продолжайте только после успеха обеих проверок. Файлы выпуска обновляются
   неатомарно: при несовпадении повторите listing/download той же папки runtime/.
   Ошибка доставки не является основанием переходить к восстановлению исходников.

3. Сохраните проверенные runtime и checksum **в обоих каталогах: /mnt/data и /home/oai**:

   ```bash
   set -eu
   for runtime_dir in /mnt/data /home/oai; do
     cp "$GDI_UPDATE_DIR/gdi.pyz" "$GDI_UPDATE_DIR/gdi.pyz.sha256" "$runtime_dir/"
     (cd "$runtime_dir" && sha256sum --check --strict gdi.pyz.sha256)
   done
   GDI_RUNTIME="/mnt/data/gdi.pyz"
   python3 "$GDI_RUNTIME" --version
   python3 "$GDI_RUNTIME" agent --help
   ```

   Обновляйте обе копии при каждом обновлении. Прочитайте скачанный agent.md:
   это инструкция опубликованного runtime. Во всех следующих командах используйте
   python3 "$GDI_RUNTIME"; прежний вызов python3 -m gdi из mount/checkout может
   продолжить запускать старый инструмент. Исходную mount-копию сохраняйте.

4. Сообщите источник runtime/, head из runtime.json, проверенный SHA256 и фактический
   GDI_RUNTIME. Обновление завершено, когда следующие вызовы используют этот файл.
   Если одна копия исчезла, проверьте оставшуюся по сохранённому checksum и восстановите
   из неё вторую. Если исчезли обе, повторите скачивание готового runtime.
   Две копии не гарантируют сохранность при материализации контейнера.

Версия вроде 0.4.0 может совпадать у разных выпусков. Идентификаторы runtime —
его SHA256 и head в runtime.json. Head описывает commit, из которого собран этот
выпуск; он **не обязан совпадать с текущей вершиной Git-ветки**. Ветка может
продвинуться после публикации runtime. Это само по себе не ошибка и не требует
скачивать manifests, восстанавливать checkout, сравнивать diff или пересобирать
утилиту. Цель обычного обновления — опубликованный runtime, выбранный владельцем.

Если runtime недоступен или проверка повторно не проходит, сообщите конкретную
ошибку доставки/проверки и сохраните прежнюю рабочую копию. Не заменяйте обновление
инструмента самостоятельным аудитом или сборкой исходников.

## Снимок коннектора

Для Git-публикации скачайте свежие `repository.json` и **все manifests выбранной
ветки**, получив полный listing папки проекта, `branches` и выбранной ветки.
Если ветка отсутствует в полном listing `branches`, её listing не нужен.
Корень проекта должен содержать каталоги `branches` и `bundles`.
Для проверки загруженного bundle дополнительно нужны полный listing `bundles`
и скачанные bytes проверяемого bundle. Для clone — checkpoint и все нужные дельты.

### Общий корень, путь проекта и папка назначения

Перед созданием снимка определите **общий корень обмена**, который использует
worker (`remote_url` в его конфигурации), и его реальный Drive folder ID. В этом
корне находятся общие `inbox/` и `ci/workers/`. Папка проекта — отдельный каталог
внутри этого корня; она содержит `repository.json`, `branches/` и `bundles/`.

`repository_path` — путь **от общего корня до папки проекта**. Он не включает
имя самого общего корня, rclone remote, Drive URL/ID или локальный путь checkout.
Вложенные каталоги между общим корнем и проектом включаются. Абстрактные примеры:

| Общий корень worker | Полный адрес проекта | `repository_path` |
| --- | --- | --- |
| `gdrive:exchange` | `gdrive:exchange/my-project` | `my-project` |
| `gdrive:exchange` | `gdrive:exchange/repos/my-project` | `repos/my-project` |
| `gdrive:exchange` | `gdrive:exchange/exchange` | `exchange` |

Последняя строка намеренно использует одинаковые имена корня и проекта:
в относительном пути остаётся только каталог проекта. Например, при общем корне
`gdrive:exchange` значение `exchange/my-project` направит worker в
`gdrive:exchange/exchange/my-project`, а не в `gdrive:exchange/my-project`.

Для CI самого GDI общий корень — `gdrive:gdi`, папка репозитория —
`gdrive:gdi/gdi`, поэтому **`repository_path: "gdi"`**. Значение `"gdi/gdi"`
ошибочно: worker добавит его к общему корню и попытается прочитать
`gdrive:gdi/gdi/gdi/repository.json`, получив `404`.

**Запросы CI для других репозиториев оформляйте по тем же правилам.** Меняются
папка проекта, Repository ID, ref/HEAD и параметры конкретного запроса; путь
проекта всегда считается относительно общего корня выбранного worker.
Примеры для общего корня `gdrive:gdi`:

| Репозиторий | Полный адрес проекта | `repository_path` | Папка job от общего корня |
| --- | --- | --- | --- |
| GDI | `gdrive:gdi/gdi` | `gdi` | `gdi/ci/jobs/JOB_ID` |
| serialterminal | `gdrive:gdi/serialterminal` | `serialterminal` | `serialterminal/ci/jobs/JOB_ID` |
| lora-sack | `gdrive:gdi/lora-sack` | `lora-sack` | `lora-sack/ci/jobs/JOB_ID` |
| Вложенный проект | `gdrive:gdi/repos/my-project` | `repos/my-project` | `repos/my-project/ci/jobs/JOB_ID` |

Во всех этих случаях уведомление направляется в `gdrive:gdi/inbox`, а registry
worker читается из `gdrive:gdi/ci/workers`. Реальные пути и Repository ID
подтвердите через listing и `repository.json` перед подготовкой плана.

Для `repository_path: "my-project"` все пути плана разрешаются так:

```text
COMMON_ROOT_FOLDER_ID/                  # общий корень: gdrive:exchange
    inbox/EVENT_ID-EVENT_SHA256.json     # уведомления всех проектов
    ci/workers/WORKER_ID/status.json     # общий статус host
    my-project/                         # PROJECT_FOLDER_ID
        repository.json
        bundles/BUNDLE_NAME
        branches/BRANCH_NAME/MANIFEST_NAME
        ci/jobs/JOB_ID/
            submission.json
            status.json
            result-<sha256>.zip
```

Перед prepare пройдите коннектором от **COMMON_ROOT_FOLDER_ID** каждый сегмент
`repository_path`, используя ID дочерней папки из полного listing родителя.
Конечный ID должен совпасть с `repository_folder_id` снимка; скачанный
`repository.json` должен содержать согласованный Repository ID. Одна лишь
проверка snapshot не доказывает правильность пути относительно общего корня:
GDI получает `repository_path` от агента и переносит его в план и уведомление.

`files.*.target_folder` в плане тоже считается от **общего корня**, уже содержит
нужный префикс проекта и не требует добавления `repository_path` второй раз.
Например, `my-project/ci/jobs/JOB_ID` разрешайте от COMMON_ROOT_FOLDER_ID,
а не от PROJECT_FOLDER_ID. Уведомление публикуется в общий `inbox`, не в inbox
проекта. Имена и bytes берите из плана; не переписывайте готовые immutable JSON.

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
python3 "$GDI_RUNTIME" agent snapshot \
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
python3 "$GDI_RUNTIME" agent clone --snapshot "$EXCHANGE_DIR/source/snapshot.json" \
  --repository-id "$REPOSITORY_ID" --destination "$WORK_DIR"
```

Каталог назначения должен отсутствовать. Проверка/восстановление проходит во
временном каталоге и quarantine; существующие файлы не заменяются.
При bundle GC достаточно последнего доступного checkpoint и дельт от него до tip;
все manifests цепочки всё равно нужны. Проверяется fast-forward от `previous`.
После clone прочитайте инструкции проекта. Для разработки сначала создайте отдельную
work-ветку по следующему разделу. Автоматического фонового commit или таймера нет:
агент явно вызывает checkpoint и публикует его через коннектор.

## Отдельная work-ветка и checkpoints разработки

Для новых work-команд используйте обновлённый runtime GDI. Активацию
work lifecycle в существующем remote выполняет оператор после обновления всех
клиентов/worker; новые remote через `remote create` уже имеют `work_version=1`;
агент не меняет repository.json самостоятельно. При `work_version:1` новый snapshot
должен дополнительно содержать полный listing `work/retired` (все страницы) и download
retirement record выбранного Work-ID, если он есть. На старте новой работы listing
нужен для проверки, что ID не retired; пустой/отсутствующий snapshot listing нельзя
подменять предположением «ветка свободна». При CI по архивной work-ветке требуется
сам retirement record с сохранённой chain; bundles скачиваются только по штатной
потребности восстановления. Не создавайте эти records вручную.


Для задачи разработки создавайте checkpoints **в отдельной `gdi-work/<WORK_ID>`**,
а не в исходной ветке оператора. Work-ID выдаёт оператор/coordinator: это отдельный
номер работы, не TODO-ID и не CI job ID. Не вычисляйте свободный ID через локальный
`max + 1` и не переиспользуйте ID после потери checkout. Несколько агентов получают
разные Work-ID и work-ветки, даже если target одна.

После clone исходной ветки и чтения её инструкций получите свежий snapshot именно
этой ветки: его HEAD должен совпадать с локальным HEAD. Создайте work-ветку через
**offline интерфейс агента**, без rclone и настройки `.gdi` remote:

```bash
python3 "$GDI_RUNTIME" agent work start "$WORK_ID" --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/assigned-target/snapshot.json" \
  --repository-id "$REPOSITORY_ID"
```

Команда требует чистый checkout, запоминает фактические target ref и BASE SHA,
отказывается переиспользовать видимый на Drive Work-ID и переключает только рабочий
checkout агента на новую work-ветку. `dev`, `main` или другая target не зашиты в код.
Git identity должна быть настроена по указаниям владельца проекта; не подставляйте
чужие author/name/email. Новый work workflow не расширяет scope задачи, не разрешает
GitHub push, изменение firmware, запуск CI/аппаратуры или deploy.

Примерно каждые 10 минут активной работы, а также перед долгим/рискованным этапом
проверьте diff и явно stage-ите нужные файлы обычным Git. Подготовьте UTF-8 body-файл
вне tracked файлов: сделано, осталось, реально выполненные проверки и ограничения.
Затем создайте checkpoint:

```bash
python3 "$GDI_RUNTIME" agent work checkpoint --repo "$WORK_DIR" \
  --repository-id "$REPOSITORY_ID" -m 'feat: описание результата' \
  --body-file "$EXCHANGE_DIR/checkpoint-message.txt"
```

Команда не stage-ит файлы сама, отказывается коммитить на обычной ветке и при
оставшихся unstaged/untracked изменениях. Она генерирует `[WORK.PART]` и привязку
`GDI-Work`, `GDI-Repository`, `GDI-Target-Ref`, `GDI-Base`. Не пишите эти поля/префикс
вручную. Промежуточный checkpoint может быть незавершённым: честно указывайте
NOT RUN/FAIL, не тратьте весь интервал сохранения на попытку получить зелёный CI.

**Каждый checkpoint публикуйте штатным `agent prepare → upload bundle → check →
upload manifest → accept`**, как описано ниже. Новый snapshot для prepare должен
выбирать `ref: refs/heads/gdi-work/<WORK_ID>`, а не прежний target ref. Для первой
публикации этой work-ветки chain пустая и готовится full bundle. Не используйте
операторский `gdi push` в контейнере без rclone. Локальный commit и даже upload одного
bundle ещё не подтверждают сохранность: нужен успешный accept. При ошибке явно
сообщите, какая часть осталась неопубликованной, и повторяйте сохранённый план.
Период full bundles `--checkpoint-every` — другое понятие, не десятиминутный интервал.

После материализации сначала найдите последнюю подтверждённую публикацию своей
work-ветки и восстановите **её**, а не исходную target. Проверить контекст можно так:

```bash
python3 "$GDI_RUNTIME" agent work status --repo "$WORK_DIR" \
  --repository-id "$REPOSITORY_ID"
```

Контекст и следующий номер восстанавливаются из Git commits; локальный файл первого
start не нужен. Не начинайте ту же работу с части 1 заново. Если состояние `ready`,
работа уже финализирована; `imported` означает импорт оператором. Для новой работы
нужен новый ID, не продолжение после `!`/`GDI-IMPORTED`.

Когда согласованный scope и обязательные проверки завершены, создайте финальный
checkpoint той же командой с `--final`. Он получит `[WORK.PART!]`. Полный финальный
subject/body станет сообщением squash-коммита оператора с удалением только префикса.
Не отмечайте финальным незавершённый scope или непроверенный обязательный gate.
После публикации передайте оператору Work-ID, target ref, BASE, final SHA и Publication
ID. Оператор использует `gdi import` в target; агент не выполняет squash/rebase своей
опубликованной истории. `GDI-IMPORTED` создаёт операторский импорт, не сам агент.

## Передать изменения пользователю

1. Скачайте свежий снимок metadata выбранной work-ветки. В Git-репозитории уже
   должен быть создан её checkpoint; для первой публикации work-цепочка на Drive
   ещё пустая, далее commit должен быть потомком её опубликованного HEAD.
2. Подготовьте публикацию:

   ```bash
   python3 "$GDI_RUNTIME" agent prepare --repo "$WORK_DIR" \
     --snapshot "$EXCHANGE_DIR/before/snapshot.json" --repository-id "$REPOSITORY_ID" \
     --output "$EXCHANGE_DIR/publication-plan"
   ```

   В JSON будут `files.bundle`, `files.manifest`, `files.notification`:
   `local_path`, `target_folder`, `target_name`, `sha256`, `bytes`;
   `upload_order`, `expected_previous_publication`, `expected_new_tip` и `next_step`.
   `target_folder` — логический путь **от общего Drive root**, не Drive folder ID.
   Найдите/создайте эти папки коннектором. `bundle_kind` показывает выбранный тип:
   по умолчанию передаётся incremental от опубликованного tip. Первая публикация
   и периодический checkpoint используют full, как обычный push: после 4 дельт
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
   python3 "$GDI_RUNTIME" agent check --plan "$EXCHANGE_DIR/publication-plan" \
     --snapshot "$EXCHANGE_DIR/after-bundle/snapshot.json" --repository-id "$REPOSITORY_ID"
   ```

   Только успешный ответ `safe_to_upload_manifest: true` разрешает следующую загрузку.
   Bundle проверяется по bytes/размеру/SHA256 и в quarantine; база должна оставаться
   ровно прежней. Разрешение относится к этому снимку и не блокирует конкурентный push.
4. Загрузите manifest последним. Скачайте его обратно и получите свежий **полный**
   listing выбранной ветки. В новом снимке сохраните ранее скачанные manifests
   и проверенный bundle вместе со свежими listings:

   ```bash
   python3 "$GDI_RUNTIME" agent accept --plan "$EXCHANGE_DIR/publication-plan" \
     --snapshot "$EXCHANGE_DIR/after-manifest/snapshot.json" --repository-id "$REPOSITORY_ID"
   ```

   Требуется единственный tip, ровно подготовленный Publication ID и точные bytes.
   При конкурентном продолжении/смене базы остановитесь, получите новый снимок,
   согласуйте историю Git и создайте **новый** план; старый сохраните для диагностики.
   При потере ответа manifest мог уже попасть на Drive: сначала повторите accept
   со свежим снимком, не создавайте новую публикацию и nonce.
5. При желании после accept загрузите `files.notification`: это `repository_updated`,
   которое не запускает CI. Для обычного fetch/pull уведомление не обязательно.
   Для завершённой work-ветки передайте пользователю Repository ID, Work-ID, target
   ref, HEAD, Publication ID и команду `gdi import` в исходной target-ветке. До `!`
   это промежуточное сохранение работы, не готовый результат для импорта. Обычная
   публикация вне work workflow по-прежнему получается штатным fetch/pull.

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

### CI промежуточного work checkpoint

Финальный `!` для CI не требуется. После публикации промежуточной части и успешного
`agent accept` используйте её точные Publication ID и SHA, snapshot той же work-ветки
и существующий `gdi agent ci prepare`. Дальше тот же протокол одного inbox-файла → read-back → accept → result, описанный ниже. Операторские `gdi ci …` агенту не нужны.

Worker проверяет закреплённые ref/HEAD/publication, даже если агент уже опубликовал
следующую часть. После FAIL исправляйте работу новым numbered checkpoint, публикуйте
его и создавайте новый CI request на новую публикацию. Это не `retry` прежнего SHA;
retry той же попытки остаётся отдельным механизмом. GitHub Actions не запускаются
самим checkpoint, а разрешение пользователя на CI-цикл сохраняется между итерациями.

Workflows из checkpoint должны допускать соответствующий event/ref. Например, filter
только на `main` не означает поддержку `gdi-work/*`; используйте согласованный event
вроде `workflow_dispatch`, если workflow его объявляет. GDI не подменяет work ref
исходной target, чтобы обойти filters. PASS исходной work-части не доказывает PASS
будущего squash поверх другой истории target.

### Автоматический выбор — без `--worker`

Заранее узнавать ID исполнителя и задавать `WORKER_ID` не требуется. Получите через
Drive-коннектор полный свежий снимок общей `ci/workers`: listings всех worker-папок
и downloads их capabilities/status. Сохраните его как `$EXCHANGE_DIR/workers.json`;
точный формат и правила полноты описаны в [scheduling.md](scheduling.md#агент-через-drive-коннектор).

```bash
python3 "$GDI_RUNTIME" agent ci workers --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/published/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --workers "$EXCHANGE_DIR/workers.json"
python3 "$GDI_RUNTIME" agent ci prepare --repo "$WORK_DIR" \
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
для accept. Скачанный файл capabilities выбранного host сохраните как
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
при prepare и accept. Revision выбранного профиля GDI берёт из этого файла;
скачивание неизменившегося файла прежнего worker не делает его окружение актуальным.

```bash
python3 "$GDI_RUNTIME" agent ci prepare --repo "$WORK_DIR" \
  --snapshot "$EXCHANGE_DIR/published/snapshot.json" --repository-id "$REPOSITORY_ID" \
  --publication PUBLICATION_ID --worker "$WORKER_ID" --profile full \
  --capabilities "$EXCHANGE_DIR/capabilities.json" \
  --output "$EXCHANGE_DIR/ci-plan"
```

### Передача и проверка — для обоих режимов

План содержит единственный передаваемый файл files.inbox: полный canonical JSON
с CI request и repository_path. Job ID, HEAD, Publication ID, worker и profile revision
зафиксированы. Поддерживаются --workflow, --event, --job, повторяемый --input KEY=VALUE.
Сразу после prepare ответ содержит safe_to_upload_inbox:true и upload_order:["inbox"].

1. По files.inbox.target_folder/target_name найдите общую inbox владельца. Сверьте
   repository_path плана с расположением проекта относительно общего root.
   Загрузите **один** files.inbox с точными bytes из плана. Папку ci/jobs/JOB_ID
   создаст worker; агент не загружает отдельные request.json/request.ready.
2. Получите полный свежий listing inbox и скачайте этот же файл по найденному ID.
   Сохраните inbox-proof.json: listing всех страниц и download exact bytes:

   ```json
   {
     "proof_version": 1,
     "folder_id": "INBOX_FOLDER_ID",
     "pages": [{"page_token": null, "next_page_token": null, "entries": [
       {"id": "EVENT_FILE_ID", "name": "JOB_ID-EVENT_SHA256.json", "is_dir": false, "bytes": 1234}
     ]}],
     "file_id": "EVENT_FILE_ID",
     "local_path": "downloaded-event.json"
   }
   ```

   Имена/IDs/bytes берите из фактического listing. При потере ответа upload найдите
   то же имя и переиспользуйте совпадающие bytes; не создавайте новый job/план.
3. Подтвердите передачу. Используйте **исходный snapshot и capabilities из prepare**:
   новый снимок проекта, создание папки job и промежуточный agent ci check не нужны.

   ```bash
   python3 "$GDI_RUNTIME" agent ci accept --repo "$WORK_DIR" --plan "$EXCHANGE_DIR/ci-plan" \
     --snapshot "$EXCHANGE_DIR/published/snapshot.json" --repository-id "$REPOSITORY_ID" \
     --capabilities "$EXCHANGE_DIR/capabilities.json" --inbox-proof "$EXCHANGE_DIR/inbox-proof.json"
   ```

state:accepted подтверждает совпадение read-back proof с единственным файлом плана,
а не начало исполнения. Далее проверяйте status/heartbeat своего job. Worker сохраняет
задание в ledger и архивирует один submission.json перед CI; запрос для наблюдения
своего job GDI берёт из сохранённого плана, повторные downloads запроса не нужны.

Если worker уже завершил задачу и удалил inbox-файл до read-back, проверьте тот же
job по сохранённому плану. Повтор отправки использует те же bytes/job ID; ledger
предотвращает повторное исполнение. Не создавайте новый job из-за потери ответа.
Не редактируйте worker/revision в готовом запросе. Отмена и замена выполняются
agent ci cancel/cancel-check и prepare --retry-of либо supervisor по явной политике.
Удаление inbox само по себе не отменяет исполнение.

## Отменить CI и явно назначить замену

Используйте этот порядок при разрешённой явной отмене или замене исполнителя;
для автоматической политики переноса используйте [supervisor](scheduling.md).
Действуйте по указанию пользователя; просьба только отменить job не означает
разрешения пересоздать его. Получите свежий полный снимок проекта с исходными
submission.json (если исходного запроса нет локально) и capabilities исходного worker из общего root.
Его `cancel_version: 1` подтверждает поддержку отмены, но не доступность host.
Если результат уже опубликован, сначала скачайте полный результат и его файлы:
проверенный terminal result не отменяется задним числом.

```bash
python3 "$GDI_RUNTIME" agent ci cancel --repo "$WORK_DIR" \
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
python3 "$GDI_RUNTIME" agent ci cancel-check --repo "$WORK_DIR" \
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
python3 "$GDI_RUNTIME" agent ci prepare --repo "$WORK_DIR" \
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
prepare → один upload → accept. Retry разрешается по проверенному terminal result либо
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

Для нового формата нужен **один итоговый файл** `result-<sha256>.zip`.
Внутри находятся result.json, final-status.json, полный binary build.log и все
artifacts. Chunks и отдельные файлы лога/artifacts скачивать не нужно.

1. Получите свежие полные listings `ci/jobs/JOB_ID` и родительских папок,
   Имена и Drive file IDs берите из этих listings. Собственный request уже находится
   в сохранённом плане; submission.json для его проверки повторно скачивать не нужно.
2. Если есть cancel.json, скачайте его. Если есть worker.running.json,
   включите его download: проверка отмены сверяет исходный claim/run_id.
   Если есть cancelled/, получите полный listing этой папки.
3. При действующей отмене и наличии пакета в cancelled/ скачайте этот пакет.
   Обычный поздний PASS не заменяет подтверждение отмены. Без пакета отмены
   GDI вернёт CANCEL_REQUESTED, если обычный результат не подтверждает CANCELLED.
   Для worker_cancellation_supported:false действуют правила legacy withdrawal.
4. Иначе скачайте единственный `result-<sha256>.zip` из папки job **по file ID**
   и добавьте в files снимка с реальными bytes из listing. Не распаковывайте
   пакет вручную и не добавляйте его внутренние файлы как отдельные Drive entries.
   Несколько пакетов в одной папке — ошибка, не выбирайте по дате или имени.
5. Выполните команду ниже. GDI проверит SHA256 архива из имени, схему/inventory
   ZIP, identity результата, размеры и hashes каждого файла, final status и
   blocking stages. Проверенные artifacts и полный лог будут в local_artifacts.

Если пакета ещё нет в полном listing, продолжайте наблюдение status/heartbeat.
Одного mutable PASS недостаточно. Worker может уже закончить исполнение, но ещё
упаковывать/передавать результат. Не создавайте новую попытку из-за этой задержки.
При ошибке download повторяйте передачу того же immutable пакета.

Исторические результаты до пакетной доставки остаются читаемыми: при отсутствии
пакета и наличии result.json нужны его отдельные artifacts/final status/build.log
и все прежние log-chunks с полными listings соответствующих папок. Новые jobs
их не создают. Наличие cancelled/ само по себе не доказывает завершение отмены.

```bash
python3 "$GDI_RUNTIME" agent ci result --repo "$WORK_DIR" --plan "$EXCHANGE_DIR/ci-plan" \
  --snapshot "$EXCHANGE_DIR/result/snapshot.json" --repository-id "$REPOSITORY_ID"
```

Используется тот же проверяющий код, что у обычного CI-клиента: identity запроса,
результата и final status, SHA256 пакета, SHA256/размер каждого artifact и полного
binary log, успешные blocking stages для PASS. Проверка chunks используется
только для исторических результатов старого формата.
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

Папка задачи от общего root — repository_path из сохранённого плана плюс
ci/jobs/JOB_ID/. Её создаёт worker, сохраняя submission.json; status.json появится
в этой же папке. Например, при общем корне `gdrive:exchange`
и `repository_path: "my-project"` путь задачи от общего корня —
`my-project/ci/jobs/JOB_ID/status.json`, а полный rclone-адрес —
`gdrive:exchange/my-project/ci/jobs/JOB_ID/status.json`. Не подставляйте полный
адрес в `repository_path` или `target_folder`.

Общий статус host расположен иначе: `ci/workers/WORKER_ID/status.json`
**относительно общего root**; для того же примера полный адрес —
`gdrive:exchange/ci/workers/WORKER_ID/status.json`.
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
   снимку. Неизменившиеся immutable manifests можно скопировать
   из прежнего проверенного снимка в новый каталог по правилам раздела
   «Снимок коннектора». Git bundles для одного наблюдения за CI не нужны.
   Собственный исходный запрос берётся из --plan; downloads request/ready не нужны.
   Если ci/jobs/JOB_ID ещё не создан, полный listing существующего родителя
   подтверждает отсутствие. Не создавайте вымышленные listings отсутствующих папок.
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
   listing, включая submission.json и другие файлы; их downloads не обязательны. Абсолютные local_path, `..`
   и symlinks не поддерживаются. Если status изменился между listing и download
   и размер не совпал, обновите listing/download; не исправляйте размер вручную
   и не подставляйте старые bytes.
5. По свежему listing проверьте наличие cancel.json, cancelled/ и
   result-<sha256>.zip. При появлении отмены/пакета дополните снимок по разделу
   «Проверить результат CI»: нужен один пакет, соответствующий действующему
   результату, а не отдельные artifacts/log/chunks. Одного status недостаточно.

После появления пакета скачайте его, выполните agent ci result и прочитайте
полный лог из возвращённого local_artifacts. Сообщите проверенный исход,
failed_stage/exit_code и причину падения. При первом FAIL в mutable status
сообщите его сразу, не ожидая пакета; это ещё не verified:true.

Если status ещё нет в **полном** listing, не добавляйте выдуманный файл или
download: GDI покажет `heartbeat.state: missing`. Это допустимо, например,
до начала обработки запроса. Если коннектор не смог прочитать папку/скачать
файл, сообщите сетевую ошибку и повторите чтение позже; ошибка не доказывает
отсутствие heartbeat, завершение или отмену CI.

### 4. Проверить heartbeat через GDI и прочитать текущий этап

```bash
HEARTBEAT_SNAPSHOT="$EXCHANGE_DIR/heartbeat-001/snapshot.json"
python3 "$GDI_RUNTIME" agent ci result --repo "$WORK_DIR" --plan "$CI_PLAN" \
  --snapshot "$HEARTBEAT_SNAPSHOT" --repository-id "$REPOSITORY_ID" \
  --heartbeat-timeout 600 > "$EXCHANGE_DIR/heartbeat-001/ci-observation.json"
cat "$EXCHANGE_DIR/heartbeat-001/ci-observation.json"
```

Команда проверяет identity проекта, публикации, плана,
исходного запроса из плана и соответствие скачанного status своей задаче. Не объявляйте
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
«worker сообщил FAIL; итоговый пакет ещё передаётся».
Больше не ожидайте успешного завершения этой попытки. Если нужно выяснить причину
и получить подтверждённый итог, продолжайте только получение итогового пакета
и называйте это ожиданием доставки. Если они долго не появляются, сообщите
последний status, его время, наличие пакета и ошибки коннектора вместо
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
возраст/свежесть heartbeat и что именно ожидается. Например: «job JOB_ID на
WORKER_ID проверяет HEAD_SHA; RUNNING/github-actions, heartbeat fresh, возраст
12 секунд; пакет результата ещё не опубликован». Если status уже FAIL/PASS, а result
ещё нет, сообщайте: «worker сообщил завершение исполнения; ожидаю доставку и
проверку результата», а не «CI всё ещё исполняется».

Различайте три фазы в сообщениях пользователю:

| Фаза | Что сообщать и делать |
| --- | --- |
| Worker выполняет CI | Текущие state/stage, timestamp, возраст heartbeat; продолжать свежие polls. |
| Worker уже сообщил FAIL/PASS, обязательные файлы ещё не получены | Исход уже сообщён; назвать конкретное ожидание: сборка, публикация или download итогового пакета. Не называть это ожиданием окончания сборки. |
| Полный snapshot готов | Выполнить настоящий `agent ci result`, затем сообщить state/verified, HEAD, failed_stage/exit_code и причину из лога. Одного наличия файлов на Drive недостаточно. |

При доставке сообщайте фактическую фазу: «worker сообщил FAIL; ожидаю пакет»,
«пакет скачивается» или «пакет получен, выполняю проверку GDI». Не называйте
скачивание пакета продолжающейся сборкой. Для нового результата не требуются
параллельные downloads или последовательный сбор мелких файлов. Immutable пакет
можно переиспользовать с проверкой SHA256; mutable status скачивайте заново.
Если пользователь остановил передачу, явно укажите, что verified:true ещё не получен.

Свежий heartbeat при неизменном логе не требует нового запроса или переноса.
Устаревший heartbeat, пауза публикации и исчезновение inbox тоже не разрешают
создавать дубликат CI: повтор/перенос выполняйте только по согласованной политике
через существующие cancel/retry/supervise-команды, сохраняя историю задач.

Не объявляйте фоновый мониторинг установленным, если не создали реальную задачу
в доступном инструменте. Если внешний инструмент ограничивает частоту проверок,
укажите этот инструмент и его фактическое ограничение. В gdi нет ограничения
«проверять не чаще раза в час». При отсутствии фонового механизма сообщите
проверенное текущее состояние; обещание уведомить позднее не заменяет мониторинг.
