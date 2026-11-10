# Сборщик агентского snapshot

`gdi agent snapshot-build` — offline-переход от сохранённых ответов коннектора
к существующему repository snapshot v1. Обычные команды оператора, rclone
transport и формат файлов на Drive остаются прежними. Команда доступна в исходниках;
агент получает её после отдельной публикации обновлённого runtime.

## Порядок работы

1. Определите доверенные Repository ID, URL папки проекта, `repository_path`
   относительно общего корня и точный `ref` по канонической [инструкции](agent.md).
2. Коннектором получите свежие полные listings необходимых папок. Сохраните
   каждый ответ без переписывания полей: полный tool result с `structuredContent`
   либо непосредственно неизменённый объект `structuredContent`.
3. Скачайте `repository.json` и все manifests выбранной ветки через raw `fetch`
   с `download_raw_file=true, include_base64=false`. Материализуйте bytes из
   выданного коннектором file reference; сохраните raw fetch response рядом.
   Не заменяйте исходный файл извлечённым текстом или заново сериализованным JSON.
4. Создайте capture v1 с путями к этим ответам и скачанным файлам. Используйте
   точные request arguments; entries, IDs, sizes и `files` snapshot не переписывайте.
5. Выполните сборщик:

   ```bash
   python3 "$GDI_RUNTIME" agent snapshot-build \
     --capture "$EXCHANGE_DIR/source/capture.json" \
     --repository-id "$REPOSITORY_ID" \
     --output "$EXCHANGE_DIR/source/snapshot.json"
   ```

Сборщик сам нормализует записи, сопоставляет fetch metadata с listing и проверяет
результат штатным `Snapshot`: identity, manifests, цепочку и prerequisites.
Дополнительный `agent snapshot` сразу после успешной сборки не нужен: результат
содержит ту же сводку. Самостоятельно повторять её криптографические проверки нельзя.

`required_bundles` задаёт необходимые checkpoint/дельты. Для clone скачайте только
их, найдя file IDs по точным именам в сохранённом полном listing `bundles`.
Сохраните fetch responses, добавьте download records в новый capture и создайте
новый snapshot filename, например `snapshot-clone.json`. `agent clone` проверяет
содержимое необходимых bundles и Git objects. Не приписывайте ему проверку
содержимого всех старых bundles.

## Capture v1

Пути к responses и materialized downloads относительны каталогу capture. Output
должен быть новым файлом в том же каталоге: это сохраняет смысл `local_path`
существующего snapshot v1. Входы, старые snapshots и скачанные bytes не меняются.

```json
{
  "capture_version": 1,
  "adapter": "google-drive-list-folder-v1",
  "repository_path": "my-project",
  "repository_url": "https://drive.google.com/drive/folders/PROJECT_FOLDER_ID",
  "ref": "refs/heads/dev",
  "listings": [
    {
      "request": {
        "url": "https://drive.google.com/drive/folders/PROJECT_FOLDER_ID",
        "top_k": 100
      },
      "response": "responses/project-list.json"
    },
    {
      "request": {
        "url": "https://drive.google.com/drive/folders/BRANCHES_FOLDER_ID",
        "top_k": 100
      },
      "response": "responses/branches-list.json"
    }
  ],
  "downloads": [
    {
      "response": "responses/repository-fetch.json",
      "local_path": "downloads/repository.json"
    }
  ]
}
```

Пример показывает пустой remote. Для существующей ветки нужны её listing и все
manifests; для clone — listing `bundles` и необходимые downloads. Для lifecycle и
CI добавляются listings/downloads, требуемые соответствующей командой. Сборщик
repository snapshot не заменяет отдельные workers registry и inbox/download proofs.

## Поддерживаемый адаптер

`google-drive-list-folder-v1` проверен по форме ответов Google Drive connector
2026-11-11. Он принимает следующие обязательные поля:

- `list_folder`: `files[]`, в каждом элементе `id`, `title`, `mime_type`,
  `file_or_folder`, `size`, `url`. Folder size — null; file size — неотрицательное
  целое либо десятичная строка. `parent_ids`, когда присутствует не-null,
  должен включать запрошенную папку. URL и ID должны совпадать.
- Raw `fetch`: `id`, `file_name`, `file_size_bytes`, `mime_type`, `url`, `file_uri`.
  Inline base64/text не заменяют materialized bytes. ID, имя, MIME type и размер
  должны соответствовать listing, локальный размер — fetch metadata.
- Неизвестные дополнительные поля metadata отдельных файлов не влияют на сборку.
  Неизвестные поля envelope listing отклоняются: они могут менять смысл полноты. Исчезновение
  обязательных полей, противоречивые значения и новая структура — ошибка адаптера.

Текущий `list_folder` не предоставляет интерфейса продолжения по страницам.
Этот профиль принимает явный `top_k` от 1 до 100 и только ответы с числом entries
строго меньше лимита. Достижение лимита, `truncated`, `has_more`,
`incompleteSearch` или непустой pagination token приводят к отказу. Для больших
папок требуется адаптер к полному пагинируемому источнику; увеличивать лимит
или рисовать пустую последнюю страницу в обход ошибки запрещено.

Сборщик проверяет согласованность сохранённых данных. Он не обращается к Drive и
не доказывает независимо полноту/свежесть ответа коннектора. Нельзя удалять entries
из raw response, фильтровать его или объявлять старые mutable bytes свежими.

## Ошибки и сохранность данных

При ошибке команда возвращает exit 1 и JSON с `kind: SNAPSHOT_BUILD_ERROR`,
`code`, `stage`, `source`, `inputs_preserved: true`, `snapshot_written: false`.
Примеры codes: `UNSUPPORTED_ADAPTER`, `UNSUPPORTED_RESPONSE`,
`INCOMPLETE_LISTING`, `INVALID_IDENTITY`, `DOWNLOAD_MISMATCH`, `INVALID_DATA`.
Ошибки tool result не считаются пустым успешным listing. Raw bearer/download URLs
и содержимое ответов не включаются в диагностический текст.

Агент останавливает зависимые clone/prepare/check/accept и сообщает оператору:

> Не удалось собрать snapshot из данных коннектора: CODE, STAGE, SOURCE и причина.
> Исходные ответы сохранены в CAPTURE_DIR. Требуется исправить получение данных
> либо обновить адаптер GDI.

Не переписывайте IDs вручную, не угадывайте переименованные поля и не собирайте
snapshot своим Python-скриптом в обход отказа. Восстановление после исправления —
новый capture/новый output; прежние evidence сохраняются. Полный raw response может
содержать временные ссылки доступа: передавайте его только в доверенную диагностику,
не публикуйте его в исходниках или чате без очистки.

## Сводка результатов

`snapshot-build` и `agent snapshot` сохраняют прежние поля и добавляют `validation`:

- `manifests_validated` — число проверенных манифестов выбранной ветки;
- `bundles_required` — число bundles для восстановления;
- `required_bundles_present` — сколько необходимых bundles материализовано;
- `bundle_contents_verified: false` — сборка snapshot не проверяет bytes bundles;
- `drive_freshness_verified: false` — независимого запроса к Drive нет.

Успешный `agent clone` возвращает `validation.manifests_validated`,
`bundles_verified`, `git_objects_verified: true`, `drive_freshness_verified: false`.
Корректный отчёт: «Цепочка: 47 manifests; восстановление: 2 bundles проверены».
Не используйте «47/47, OK» как утверждение о bytes всех файлов на Drive.

Локальные фазы доступны через существующий `--profile-log`: `adapt connector responses`
и `validate assembled snapshot`. Время внешних запросов/скачиваний измеряет сторона
коннектора; сборщик не выдаёт время локального разбора за время загрузки из Drive.
