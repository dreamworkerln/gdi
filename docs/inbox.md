# Общая inbox через rclone

Worker config version 2 содержит один `remote_url`, например `gdrive:gdi`.
Списка проектов, branches, profiles и команд CI в нём нет. На один корень назначается
один worker. Имена rclone remotes на разных машинах могут отличаться; они должны
указывать на одну и ту же папку Drive.

```text
gdrive:gdi/
    inbox/<event-id>-<sha256>.json
    ci/workers/<worker-id>/capabilities.json
    ci/workers/<worker-id>/status.json
    my-project/
        repository.json
        bundles/
        updates/
        ci/jobs/<job-id>/...
    another-project/...
```

Worker в простое делает только non-recursive listing `inbox`. Он не перечисляет
папки проектов и не просматривает архив jobs. Listing — одна операция rclone;
пагинация и разрешение путей backend могут потребовать нескольких HTTP-запросов.

## Подключение проекта

```bash
gdi remote add drive gdrive:gdi/my-project --init
```

`--init` нужен только для новой пустой папки проекта. Подключение сохраняется в
`.gdi/config.json` (в linked worktrees — в `.gdi` основного worktree):

```json
{
  "config_version": 1,
  "remotes": {
    "drive": {
      "url": "gdrive:gdi/my-project",
      "repository_id": "0123456789abcdef0123456789abcdef",
      "inbox_root": "gdrive:gdi"
    }
  }
}
```

По умолчанию общий корень — родитель URL проекта. При вложенной структуре задайте
его явно: `gdi remote add drive gdrive:gdi/repos/my-project --inbox-root gdrive:gdi`.
Адрес проекта обязан находиться внутри этого корня. Локальная `.gdi` не передаётся
в commits; добавьте `.gdi/` в `.gitignore`. Push отвергает commit с tracked `.gdi`.
Установка подключения не изменяет `.git/config`, `.gitignore` или другие файлы проекта.

Старые секции `gdi.remote.*` читаются из Git config и копируются в собственный config
при первом обращении. Git config при этом не редактируется. Новый config имеет
приоритет, включая удалённые локальные remotes. Кеш создаётся заново в `.gdi/cache`;
старый CI outbox/results копируются из `<git-common-dir>/gdi-ci` при первом CI-вызове,
чтобы повтор отправки сохранил job ID. Старые каталоги автоматически не удаляются.
Миграция использует atomic copy/fsync каждого файла и completion marker последним.
После crash она повторяется, даже если новая папка уже существует. Совпадающие файлы
не копируются заново; конфликтующие bytes дают ошибку с сохранением обеих копий.
На время миграции остановите старые клиенты, записывающие legacy state.

Клиент читает общие capabilities и переходит на legacy путь только при доказанном
отсутствии файла (для rclone — stat exit 3/4). Повреждённый JSON, ошибка прав или
сети передаются вызывающему клиенту и не скрываются fallback.

## Уведомления

Каждая запись immutable, schema `inbox_version: 1`, canonical JSON UTF-8 не больше
1 MiB. Имя содержит ID и SHA256 **точных bytes**. Поля:

- `event_id`: 32 lowercase hex;
- `type`: `repository_updated` или `ci_requested`;
- `repository_id`: 32 lowercase hex;
- `repository_path`: путь относительно общего корня;
- `ref`, `head`, `publication_id`: branch ref, точный Git SHA и ID публикации;
- `worker_id`, `job_id`, `request_sha256`: CI identity; для обычного push — null.

В пути запрещены absolute paths, `..`, пустые компоненты, backslash, colon и выход
в служебные `inbox`/`ci`. Worker проверяет `repository.json` по указанному ID,
branch ref и соответствие publication/request. Уведомление не содержит argv или
произвольных адресов rclone: worker использует собственный настроенный root.

Обычный push публикует `repository_updated` после проверенной Git publication.
Worker проверяет ссылку, сохраняет durable receipt и удаляет уведомление. Это
подтверждение доставки публикации; рабочие ветки пользователя не обновляются и
CI автоматически не запускается. Повтор push того же HEAD может повторно разместить
ту же запись; receipt позволяет безопасно подтвердить её снова.

CI submit: durable local outbox → request upload → request.ready → inbox **последней**.
CI event ID равен job ID; event содержит hash request. Разные агенты создают разные
имена файлов и не выполняют read-modify-write общего индекса. Порядок listing не
задаёт порядок commits; каждый запрос закрепляет собственный SHA.

Пустая/частичная JSON, неверный checksum, отсутствующий ready или несоответствие
identity не приводят к исполнению или удалению уведомления. Ошибка остаётся в journal;
следующий цикл повторяет чтение. Устойчивую повреждённую запись диагностирует оператор.
Она не блокирует обработку других корректных записей.

Для CI worker сохраняет request, маршрут проекта и имя уведомления в SQLite ledger.
После restart готовый result только допубликовывается. RUNNING без durable result
даёт INTERRUPTED; автоматического повторного запуска нет. После result upload и
read-back verification ledger становится PUBLISHED, затем удаляется только конкретная
запись inbox. Потерянное подтверждение удаления обрабатывается повторно без CI.
При pending jobs нельзя менять worker root или удалять ledger.

Уведомления не являются блокировкой публикаций Git. Конкурирующие push одной ветки
по-прежнему могут дать конфликт metadata; exactly-once и multi-worker не обещаются.
Drive Changes API, push notifications и PubSub оставлены последующим версиям.
