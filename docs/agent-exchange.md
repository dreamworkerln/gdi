# Единый Git packet protocol v4 (GDI 0.8)

Клиенты CLI, worker и agent runtime обновляются вместе. Обратная совместимость
обычного push/pull с v3 не поддерживается; v3 reader используется только для
явного upgrade и исторических regression tests. Repository ID и Git SHA сохраняются.

## Хранилище

- repository.json: version=4, repository_id, object_format=sha1, work_version=1.
- heads/<encoded-branch>.json — изменяемый указатель ветки.
- packets/<sha256>.zip — единственная immutable форма публикации для всех клиентов.
- work/imports и work/retired — явный work workflow.
- ci/ и общая inbox/ — отдельные CI и агентские запросы.

ZIP содержит ровно metadata.json и source.bundle (ZIP_STORED). Metadata:
packet_version=1, repository_id, repository_path, ref, head, previous,
previous_head, worker_id, request_id, bundle_sha256, bundle_bytes.
Bundle всегда полный. previous/previous_head — ожидаемая база или обе null.
Request ID входит в пакет до вычисления его digest. Publication ID — SHA256 ZIP.
Указатель: head_version=1, repository_id, ref, publication_id, packet_bytes,
metadata (точная копия metadata.json). Дополнительный manifest не загружается.

## Успешный путь и стоимость

Операторский push: read head → upload packet → update head. Ноль read-back,
повторных repository/chain checks, export, checkpoint, archive, notification или
очистки. При отсутствии head один раз читается repository.json для проверки первого
push. Неизменившийся HEAD, включая -f, требует только чтения указателя.
-f совместим как spelling: каждый v4 пакет уже полный, отдельного checkpoint нет.

Fetch выбранной ветки: read head → download packet (если нет локального кеша).
Clone дополнительно один раз читает repository.json при подключении; --branch
избавляет от обхода остальных веток. Без --branch нужен один listing heads/.
Status/log читают только указатель текущей ветки. log показывает текущую публикацию;
история Git-коммитов доступна обычным git log, без обхода manifests на Drive.

Агент pull получает тот же head и ZIP. Agent push сохраняет локальный план и
загружает один ZIP в общую inbox. Имя:
agent-push-<repoID32>-<requestID32>-<publicationID64>-<workerID>.zip.
Маршрут worker встроен в пакет; при отсутствии — выбирается из registry один раз.
Worker скачивает входной пакет для локальной проверки Git, читает текущий head,
перемещает исходный файл в packets/ на том же backend, обновляет head и отправляет
небольшую квитанцию. Он не собирает и не загружает Git-данные второй раз.
При первом head дополнительно читается repository.json. Переименование/перемещение
rclone использует возможности backend; конкретное число HTTP-вызовов и возможность
server-side move необходимо проверить отдельно на реальном Drive.

Это бюджеты логических файловых операций GDI, не HTTP-трассировка rclone. Поиск
неизвестных Drive ID коннектором добавляет вызовы. Не обещается ровно три Google API
запроса на push. Полный bundle увеличивает байты с ростом Git-истории, но не число
скачиваемых manifests. Старые packets остаются до отдельного явного GC.

## Проверки и параллельность

ZIP/SHA/Git/ref/head/FF проверяются локально при получении. Успешный ответ транспорта
считается подтверждением записи; файл обратно не скачивается. Это не обещание,
что тип удалённого объекта заранее проверяется отдельным stat: CLI читает metadata
через один cat, затем GDI разбирает полученное содержимое локально. Это также не обещание,
что Git выявит потерю обновления: файлы Drive не имеют здесь CAS-транзакции.
Независимые писатели одной ветки должны работать последовательно. Worker
сериализует адресованные ему запросы; ожидаемая база при приёме сравнивается один раз.
Гонка независимых CLI может изменить последний head; immutable пакеты и локальные
планы сохраняют данные для восстановления. Многократные старые проверки тоже не
обеспечивали атомарность. Отсутствие read-back — явно согласованный контракт.

## Повторы и сбои

Native plan: PREPARED → UPLOADING → UPLOADED → PUBLISHED. После неудачи head upload
повтор использует тот же ZIP. Неоднозначный upload допускает один адресный stat
пакета только при recovery. Нормальный успешный путь не делает этого stat.
Worker journal: PREPARED → MOVING → MOVED → REPLIED → DONE. После server-side move
inbox может исчезнуть: незавершённые запросы восстанавливаются из локальных journal.
При неоднозначном move проверяется наличие целевого packet; после MOVED повторный
upload/перемещение не нужен. При потере ответа после head update совпадение packet ID
в head подтверждает тот же запрос. Квитанция сохраняется до upload и повторяется
тем же содержимым. При CONFLICT исходный пакет не публикуется вместо чужой ветки.
Агент не опрашивает Drive в цикле и сообщает SUBMITTED до получения PUBLISHED.

## Переход и GC

`gdi remote upgrade NAME --quiescent --all-clients-updated` читает старые v3 refs,
формирует по одному полному пакету для активной ветки и создаёт pointers, затем
последним обновляет repository.json до version=4. IDs/коммиты сохраняются, old files
не удаляются, текущая ветка/checkout оператора не меняются. Локальные планы дают
возможность повторить прерванный переход. Изменения Drive этим документом не разрешены.

GC — только отдельная команда, --apply требует остановленных писателей/worker.
Защищаются текущие/сохраняемые пакеты, CI и work references. Никакой автоматической
очистки runtime, старого v3 namespace или пользовательских данных при push нет.
Новое retirement work metadata имеет work_version=2 и список packets; исходные
import receipts сохраняются. Retired ID не воскрешается.

CI v4 не скачивает свой результат обратно после upload. Consumer проверяет пакет
при его фактическом получении. Native push сам по себе CI или worker-задачу не создаёт.
Runtime deploy проверяет zipapp локально, загружает gdi.pyz/runtime.json/agent.md
один раз без обратного скачивания; лишний повтор native push из deploy.sh удалён.
