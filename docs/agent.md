# GDI 0.8: агент с Google Drive-коннектором

Все CLI, worker и runtime должны быть версии 0.8 и работать с Git protocol v4.
Старые инструкции 0.7 с export, manifests, checkpoints и prepare/check/accept
для обычного Git-обмена больше не используются. Нужны Python 3.10+ и Git на Linux.
GDI агента работает offline; файлы переносит коннектор, без rclone в контейнере.

## Разрешения и ветка

Работайте в репозитории и ветке пользователя. Не создавайте и не переключайте
ветки без явного разрешения. Обычные коммиты сохраняются без squash/rebase.
**Work-ID не нужен**: не требуйте и не подставляйте его по умолчанию.
Work workflow — отдельное согласование: назвать `gdi-work/<ID>`, объяснить squash
при import, получить разрешение. Назначение ID само по себе не разрешает ветку.
Коммиты, тесты, публикация и CI требуют разрешений проекта/задачи.

При ошибке сообщите исходную диагностику и сохраните планы. Не начинайте
самостоятельный аудит или исправление GDI, коннектора и ОС. Не используйте
Google Docs, шифрованные документы, самодельные мосты или ручные manifests.

## Runtime и подключение

Один раз получите `gdi.pyz`, `runtime.json`, `agent.md` из доверенной папки
`gdi/gdi/runtime/` на Drive. Локально проверьте SHA256/размер zipapp по runtime.json.
Не скачивайте runtime перед каждой операцией. Исходники GDI для установки не нужны.
Специальная инструкция CI/work включена в zipapp как `agent-reference.md`.

Repository ID и ветка задаются пользователем или доверенной конфигурацией. Если ID
неизвестен, один раз прочитайте repository.json и закрепите первое доверие.
Не берите ожидаемый ID из проверяемого пакета. При несовпадении остановите обмен.
Если repository.json имеет version=3, оператор должен выполнить явный upgrade
на хосте; агент не мигрирует хранилище и не переключается на старый протокол.

Сохраняйте Drive ID папок и указателя ветки. Изучение других репозиториев и
полный обход Drive не нужны. Обменные файлы и логи держите вне проекта, например
в `/mnt/data/exchange` или `/tmp`. `.gdi` обслуживает сам GDI.

## Pull: указатель и один пакет

Указатель: `<repository>/heads/<encoded-branch>.json`. Имя ветки кодируется:
`%` → `%25`, затем `/` → `%2F`; например dev → dev.json,
feature/test → feature%2Ftest.json. После первого поиска используйте сохранённый
Drive ID указателя.

1. Скачайте этот небольшой JSON один раз в raw-режиме.
2. Из publication_id получите точное имя `packets/<publication_id>.zip`.
   Найдите этот файл адресно и скачайте raw один раз. Уже имеющийся локальный
   пакет с тем же ID можно использовать повторно.
3. Выполните offline-команду с материализованными файлами:

```bash
python3 /mnt/data/gdi.pyz agent pull \
  --head /mnt/data/exchange/head.json \
  --package /mnt/data/exchange/packet.zip \
  --repository-id TRUSTED_REPOSITORY_ID \
  --destination /mnt/data/checkouts/project
```

Для существующего checkout замените --destination на --repo. GDI применяет FF,
сохраняет local-ahead, отказывает при divergence/грязном checkout/другой ветке.
Суффикс `.bin` в локальном имени от коннектора допустим: проверяется содержимое.
Не скачивайте manifests, bundles, archives и checkpoint.json. Не создавайте capture
или snapshot-build. Локальные Git-проверки выполняет GDI; не повторяйте их вручную.

## Push: один inbox ZIP и квитанция

После разрешённых коммитов:

```bash
python3 /mnt/data/gdi.pyz agent push --repo /mnt/data/checkouts/project \
  --repository-id TRUSTED_REPOSITORY_ID --output /mnt/data/exchange/push-001
```

При ALREADY_PUBLISHED не обращайтесь к Drive. При PREPARED загрузите только
`files.package.local_path` в общий `<gdi-root>/inbox/` с точным target_name.
Не загружайте отдельно указатель, manifest, checkpoint или notification.
Не скачивайте загруженный ZIP обратно и не перечитывайте папки для проверки.

Worker указан в metadata базы. Если маршрута нет, один раз найдите совместимый
worker в общем `ci/workers`, сохраните его ID и capabilities с git_packet_version=1
и agent_exchange_version=2. Используйте --worker ID при подготовке пакета.
Агент выбирает worker по доступной конфигурации/registry; Work-ID для этого не нужен.
Не просите пользователя придумывать worker ID, если он уже доступен в registry.

После upload сообщайте «отправлено worker». Подтверждение публикации пока не получено.
Worker скачивает пакет для Git-проверки, перемещает тот же файл внутри Drive в
целевой packets/ и обновляет указатель. Он не создаёт второй ZIP и не загружает
Git-данные повторно. CI автоматически не запускается.

Подождите локально, затем один раз найдите/скачайте квитанцию по receipt_path
относительно общего корня GDI. Примените её к сохранённому плану:

```bash
python3 /mnt/data/gdi.pyz agent push --repo /mnt/data/checkouts/project \
  --repository-id TRUSTED_REPOSITORY_ID --output /mnt/data/exchange/push-001 \
  --receipt /mnt/data/exchange/receipt.json
```

Только PUBLISHED подтверждает HEAD и publication_id. CONFLICT означает, что база
изменилась: получите актуальные указатель/пакет и согласуйте объединение истории.
Не выполняйте force, squash или смену ветки самостоятельно.

Если квитанции ещё нет, сохраните план, сообщите «отправлено, подтверждения нет»
и продолжайте независимую работу. Не опрашивайте Drive в цикле. Выключенный worker
означает отложенную публикацию. Повторяйте upload только при подтверждённой
недоставке, теми же байтами и именем. При новом HEAD используйте новый output.

## Стоимость

| Операция | Файловые операции через коннектор |
| --- | --- |
| Pull свежего состояния | JSON указателя + один ZIP |
| Push | Один upload ZIP + получение одной квитанции |
| Нет новых коммитов | Ноль |

Поиск неизвестного Drive ID добавляет вызовы. Это не бесплатная операция.
Полный bundle содержит Git-историю: рост истории увеличивает байты, но не число
manifests/скачиваний. Встроенные обращения rclone на хосте учитываются отдельно.
Оператор и другие независимые писатели одной ветки должны работать последовательно:
Drive/rclone не предоставляют здесь транзакционный compare-and-swap.

## CI и work — только по отдельному заданию

Извлеките справочник локально, без нового fetch:

```bash
python3 -c 'import sys,zipfile; print(zipfile.ZipFile(sys.argv[1]).read("agent-reference.md").decode())' /mnt/data/gdi.pyz
```
