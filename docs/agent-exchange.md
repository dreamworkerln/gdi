# Agent exchange v1 — GDI 0.7

Это новый несовместимый агентский интерфейс поверх native Git protocol v3.
CLI, worker и runtime обновляются вместе. Repository ID, коммиты, SHA и native
история сохраняются. Старые планы snapshot prepare/check/accept не становятся
новыми пакетами; специальные CI/work интерфейсы сохранены отдельно.

## Транспорт и стоимость

Агентский GDI работает offline. Коннектор переносит один ZIP export при pull,
один ZIP request при push и одну JSON-квитанцию после обработки worker.
Неизвестные Drive ID требуют дополнительного точного поиска; известные ID
используются повторно. Нет обратного скачивания upload, перечитывания manifests,
сборки capture и проверочных snapshots на стороне агента. Подготовка неизменившегося
HEAD возвращает ALREADY_PUBLISHED без обращения к Drive.

Это минимизация **агентских** вызовов коннектора, а не обещание нулевого host I/O.
Worker использует обычный rclone/native protocol, локальные проверки Git и
checkpoint/архивирование. Каждый пакет содержит полный bundle: в обмен на
постоянное число файлов передаётся больше байт, чем у incremental native push.

## Export

Путь: `<repository>/agent/export-<repository_id>-<sha256(ref UTF-8)>.zip`.
Файл изменяемый; copyto/copyfile обновляет тот же путь. Агент сохраняет Drive ID.
Native push готовит export из своих объектов; worker при обработке уведомления
также обновляет export и закрепляет свой worker_id. Уже подготовленный export
кешируется локально по repo/ref/head/publication/worker; `push --full` обновляет его
принудительно. Если host config worker.json v2 соответствует inbox_root, native
push может закрепить worker_id сразу. Без маршрута pull разрешён, fast push
требует обновлённого export от оператора/worker.

ZIP содержит ровно два несжатых файла: `metadata.json`, `source.bundle`.
Metadata export имеет поля:

```text
agent_exchange_version=1, kind="export", repository_id, repository_path,
ref, head, bundle_sha256, bundle_bytes, publication_id, worker_id
```

Repository path задаётся относительно общего корня GDI; ref — полный refs/heads/…;
publication_id — SHA256 native manifest; worker_id — допустимый ID или null.
Bundle полный, без prerequisites. ZIP names, размер, SHA256, Git payload и
заявленный ref/head проверяются локально GDI. Локальное имя `.zip.bin` допустимо.
Repository ID проверяется по независимо заданному ожиданию, не по имени файла.

`agent pull` создаёт checkout атомарно в новом каталоге либо требует чистой
текущей ветки. Применяет только FF; сохраняет local-ahead и отказывает при divergence.
В `.gdi/agent-base.json` сохраняются repoID/path/ref/head/publication/worker.
Это состояние инструмента, не коммитируемый проектный файл.

## Request

В общей папке inbox:

```text
agent-push-<repository_id32>-<request_id32>-<package_sha25664>-<worker_id>.zip
```

Тот же ZIP с двумя файлами; metadata push вместо publication_id имеет:
`request_id`, `base_head`, `base_publication`, `full` (bool).
`worker_id` обязателен и совпадает с адресатом в имени. Head должен быть потомком
base_head. План и точные байты ZIP сохраняются локально в --output; повтор команды
не меняет request ID/байты. Изменение HEAD требует нового каталога плана.
Worker ID берётся из export, не спрашивается у пользователя и не является Work-ID.

Worker игнорирует пакеты другого адресата. Конфликтующие имена с одинаковыми
repoID/requestID блокируются. Полученный ZIP сохраняется в worker state_dir;
checksum имени, metadata и полный bundle проверяются до публикации.
Ожидаемая base publication/head сравнивается с текущей native вершиной.
Если вершина продвинулась, возвращается CONFLICT; автоматического force/merge нет.
Если head уже опубликован, повтор подтверждается без нового Git-коммита.

Публикация идёт из отдельного object store worker `agent-publishers/<repoID>`.
Checkout и выбранная ветка оператора не меняются. Worker импортирует исходные
коммиты, вызывает native Exchange.push, затем обновляет export. Full/checkpoint
выбирает host GDI по обычным правилам, либо явно по флагу full. CI не запускается.

## Квитанции и восстановление

Путь: `<repository>/agent/receipt-<repoID>-<requestID>.json`, immutable.
Поля: agent_exchange_version, repository_id, request_id, package_sha256, worker_id,
ref, head, state. PUBLISHED дополнительно имеет publication_id; CONFLICT —
current_head/current_publication/error. Клиент допускает ERROR с диагностикой.
Только проверенная PUBLISHED обновляет локальную базу; старую квитанцию нельзя
применить поверх другой базы или другой выбранной ветки.

Worker journal проходит PREPARED → PUBLISHING → REPLIED. PUBLISHING записывается
до native push. При сбое после commit сверяется текущая и архивная цепочка по
head/previous; уже опубликованный head не публикуется повторно. При recovery
export восстанавливается из актуальной вершины, затем выдаётся квитанция.
Готовая квитанция сохраняется до upload и повторяется теми же байтами. Inbox
удаляется только после её upload. Сбой acknowledgement оставляет пакет для
идемпотентного повтора. Pending inbox requests защищают remote от GC.

Агент не опрашивает Drive в цикле. Он ждёт локально, затем получает квитанцию;
если её нет, сохраняет план и сообщает SUBMITTED, не PUBLISHED. Выключенный worker
означает задержку публикации. Неоднозначный upload не требует нового request ID:
сначала попытаться получить квитанцию, при подтверждённой недоставке повторить
тот же пакет. Случайное создание дубликатов файлов коннектором не маскируется.

## Ограничения проверки

Исходники и документация реализованы локально; тестовый прогон и реальный Drive
acceptance требуют отдельного запроса оператора. Это описание контракта, не отчёт
о состоявшемся deploy. Перед использованием обновить CLI/worker/runtime и создать
export обычным host push; пересоздавать native remote не требуется.
