# Общая inbox через rclone

Worker config version 2 содержит один `remote_url`, например `gdrive:gdi`.
Списка проектов, branches, profiles и команд CI в нём нет. У разных host общего
корня должны быть разные worker_id. Каждый CI request адресует один ID,
выбранный явно или через [политику клиента](scheduling.md). Имена rclone remotes на разных машинах могут отличаться; они должны
указывать на одну и ту же папку Drive.

```text
gdrive:gdi/
    inbox/<event-id>-<sha256>.json
    ci/workers/<worker-id>/capabilities.json
    ci/workers/<worker-id>/status.json
    my-project/
        repository.json
        bundles/
        branches/
        ci/jobs/<job-id>/...
    another-project/...
```

Worker в простое делает только non-recursive listing `inbox`. Он не перечисляет
папки проектов и не просматривает архив jobs. Listing — одна операция rclone;
пагинация и разрешение путей backend могут потребовать нескольких HTTP-запросов.

## Подключение проекта

```bash
gdi init
gdi remote create drive gdrive:gdi/my-project
```

`remote create` создаёт новую пустую папку проекта; `remote add` подключает уже
существующий remote без удалённых изменений. Подключение сохраняется в
`.gdi/config.json` (в linked worktrees — в `.gdi` основного worktree):

```json
{
  "config_version": 2,
  "default_remote": "drive",
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
в commits; `gdi init` исключает её через локальную `.git/info/exclude`.
Для проектов без `gdi init` добавьте `.gdi/` в `.gitignore`. Push отвергает commit с tracked `.gdi`.
Установка подключения не изменяет `.git/config`, `.gitignore` или другие файлы проекта.

Первое подключение выбирается по умолчанию для `gdi push`, `gdi fetch`, `gdi pull` и `gdi log`.
Сменить его можно через `gdi remote default NAME`. `gdi status` проверяет все
подключения; `gdi status NAME` — одно. Старый local config v1 и Git exchange v1/v2
не поддерживаются: сохраните старый config отдельно и пересоздайте подключения.
Git config `gdi.remote.*` больше не импортируется.

Старый CI outbox/results при необходимости копируется из `<git-common-dir>/gdi-ci`
с atomic copy/fsync и completion marker последним. Конфликтующие bytes сохраняются
и дают ошибку. Перед переносом CI state остановите записывающие его старые клиенты.

Клиент требует у выбранного worker capability ci_submission_version:1.
При отсутствии поддержки отправка завершается сообщением обновить worker;
возврата к старому request/ready-сценарию нет. Повреждённые capabilities или
ошибка сети также не скрываются fallback.

## Уведомления

Каждая запись immutable: inbox_version:1 для публикаций Git, inbox_version:2
для самодостаточного CI. Canonical JSON UTF-8 не больше
1 MiB. Имя содержит ID и SHA256 **точных bytes**. Поля:

- `event_id`: 32 lowercase hex;
- `type`: `repository_updated` или `ci_requested`;
- `repository_id`: 32 lowercase hex;
- `repository_path`: путь относительно общего корня;
- `ref`, `head`, `publication_id`: branch ref, точный Git SHA и ID публикации;
- worker_id, job_id, request_sha256: CI identity; для обычного push — null;
- request: только для inbox v2, полный CI request v2, включая workflow selector,
  profile revision и retry_of. Его canonical bytes должны совпасть с request_sha256,
  а все identity fields — с соответствующими полями оболочки.

В пути запрещены absolute paths, `..`, пустые компоненты, backslash, colon и выход
в служебные `inbox`/`ci`. Worker проверяет `repository.json` по указанному ID,
branch ref и соответствие publication/request. Уведомление не содержит argv или
произвольных адресов rclone: worker использует собственный настроенный root.

Обычный push публикует `repository_updated` после проверенной Git publication.
Worker проверяет ссылку, сохраняет durable receipt и удаляет уведомление. Это
подтверждение доставки публикации; рабочие ветки пользователя не обновляются и
CI автоматически не запускается. После успешной доставки клиент сохраняет durable receipt и не создаёт уведомление
заново при обычном unchanged push. При сбое до receipt доставка повторяется с тем
же immutable event; worker receipt позволяет безопасно подтвердить его снова.

CI submit: durable local outbox → **один inbox upload → read-back**.
Файл сам содержит полный request и маршрут; его публикация означает готовность.
Отдельных uploads request.json/request.ready и создания job-папки отправителем нет.
CI event ID равен job ID, имя содержит SHA256 всех bytes оболочки. Повтор сохраняет
те же bytes/job ID; несколько файлов с одним job ID дают конфликт до исполнения.
Порядок listing не задаёт порядок commits; запрос закрепляет собственный SHA.

Агент вызывает prepare → upload files.inbox → accept с read-back proof. Accept
использует тот же проверенный snapshot, что prepare: промежуточный snapshot задания
и ci check не нужны. Полный listing inbox и download подтверждают hash/размер/ID.
Запрос сохраняется локально для статуса и отмены ещё до первого обхода worker.
Другой обычный CLI без локального запроса может прочитать его из общей inbox.

Пустая/частичная JSON, неверный hash, несогласованные вложенные identity или поля
запроса не приводят к исполнению/удалению. Worker записывает задание в ledger,
затем архивирует тот же пакет как ci/jobs/JOB_ID/submission.json с read-back.
До подтверждения архива CI не запускается. При ошибке архивирования/перезапуске
используются прежние request/route/job ID; новый запуск не создаётся.

Для CI worker сохраняет request, маршрут проекта и имя уведомления в SQLite ledger.
После restart готовый result допубликовывается после проверки отмены. RUNNING без
durable result даёт INTERRUPTED либо CANCELLED при проверенной отмене;
автоматического повторного запуска нет. После result upload и
read-back verification ledger становится PUBLISHED, затем удаляется только конкретная
запись inbox. Потерянное подтверждение удаления обрабатывается повторно без CI.
При pending jobs нельзя менять worker root или удалять ledger.

Отмена публикуется через gdi в проектном `ci/jobs/JOB_ID/cancel.json`, привязывается
к исходному request и сохраняется локальным worker. Исчезновение inbox event
не отменяет локальную очередь. При поздней отмене подтверждение CANCELLED
доставляется в `ci/jobs/JOB_ID/cancelled/`, исходные immutable результаты остаются
историей. Если ответ удаления inbox потерян уже после PUBLISHED, recovery проверяет
и допубликовывает именно отменённый результат, затем повторяет удаление события.
Новый worker не перехватывает старый request: разрешённая замена — отдельный job
с retry_of, новым адресатом и отдельным inbox event. Форматы — [ci-protocol.md](ci-protocol.md).

Уведомления не являются блокировкой публикаций Git. Конкурирующие push одной ветки
по-прежнему могут дать конфликт metadata; exactly-once и распределённая координация
исполнения/нагрузки не обещаются.
Drive Changes API, push notifications и PubSub оставлены последующим версиям.

Переход 2026-10-25 согласован оператором без совместимости со старым альфа-worker
и многофайловыми планами отправки. Обновить CLI, worker и runtime/agent.md вместе.
Старые очереди/репозитории при необходимости обслуживаются оператором вручную;
этот переход сам не мигрирует и не удаляет существующие данные.
