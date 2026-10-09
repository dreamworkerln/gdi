# TODO gdi

## Надёжность и управление CI

- [ ] Явно показывать устаревший heartbeat; расширить диагностику окружения
  и версий инструментов.
- [ ] Добавить политику хранения локальных данных, ограничения размера spool/cache/logs
  и безопасную очистку checkout после доставки результата.
- [ ] Добавить очистку CI artifacts/logs на Drive: dry run, retention,
  защита результатов, которые ещё нужны клиентам.
- [ ] Разрешить bundle GC при незавершённых CI-заданиях через защиту нужных им bundles,
  вместо полного запрета GC.
- [ ] Добавить управляемую отмену CI-задания с остановкой процессов
  и подтверждённым конечным состоянием.
- [ ] Автоматически выбирать последний подходящий PASS без явного job ID,
  определить правила выбора при повторных проверках.
- [ ] Усилить изоляцию исполнения и обработку сбоев очистки Docker-контейнеров
  после аварийной остановки.

## Дальнейшая архитектура

- [ ] Изучить Drive Changes API/cursor для сокращения повторных listings.
- [ ] Добавить опциональные notifications/PubSub, сохранив polling.
- [ ] Поддержать несколько workers с отдельной координацией заданий.

## Сделано

### Режим агента

- [x] Снимки коннектора с реальными Drive IDs, всеми страницами и дубликатами;
  локальная проверка repository identity, manifests и полной цепочки без rclone.
  Свежесть и полноту получения с Drive обеспечивает агент через коннектор.
- [x] Gdi agent prepare/check/accept: full bundle и manifest общим ядром,
  quarantine, сохранённый JSON-план с paths/names/bytes/SHA256 и порядком загрузок.
  Повторы сохраняют bytes/nonce, изменение базы и подмена данных вызывают отказ.
  Manifest разрешается после проверки загруженного bundle; accept подтверждает
  точные bytes и единственный tip. Проверен приём обычными fetch/pull.
- [x] Gdi agent clone восстанавливает новый рабочий репозиторий из скачанных
  checkpoint/prerequisite bundles с проверкой SHA256, ref/HEAD и ancestry.
  Запуск из исходников требует только Python и Git, без setuptools/pip.
- [x] Gdi agent ci prepare/check/accept/result: общие request/capabilities/result
  валидаторы; фиксированные job ID и bytes, request → ready → inbox,
  скачивание и проверка request/ready до разрешения inbox, полный результат,
  artifacts и log chunks до PASS. Устаревшие capabilities отклоняются;
  явный retry требует проверенного terminal result. CI отделён от Git-публикации.
- [x] Инструкция использует команды GDI вместо самостоятельных Python-рецептов;
  описаны снимки, bootstrap исходников GDI, передача файлов и проверка результата.
  docs/agent.md — основной документ агента; docs/ci.md — CI через CLI с rclone.

### Обмен Git и CLI

- [x] Общее ядро публикации в gdi/publication.py: локальная подготовка full/incremental
  bundle и canonical manifest, nonce, SHA256, Publication ID и пути ветки;
  общая проверка объектов и ancestry в quarantine. Push использует это ядро,
  отдельно управляя загрузкой и свежими проверками Drive. Ядро работает без rclone,
  не создаёт commit и не меняет рабочие refs; форматы данных сохранены.
- [x] Native rclone RC в CLI и worker с ограничением жизни командой/операцией,
  свежей привязкой URL при проверке identity и повторных проверках внутри push,
  отказом при смене repository ID и остановкой RC после SIGKILL родителя.
  Listing ограничен выбранной веткой с защитой от дубликатов каталогов;
  сохранены SHA256, immutable uploads, проверки цепочек и форматы данных.
  Все RC-обращения входят в общий профиль; процессы и обращения считаются отдельно.
  Проверены сбои, длинные цепочки и пересоздание папки на Drive; повторные замеры
  сохранены в docs/profiling.md. Прежний транспорт доступен через GDI_TRANSPORT=cli.
- [x] Git protocol v3: полные checkpoints и incremental bundles,
  отдельные цепочки веток и prerequisite chains.
- [x] Читаемые каталоги веток на Drive: branches/<encoded-branch>/;
  обратимое кодирование slash/percent, сохранение Unicode.
- [x] Проверки SHA256, размера, repository ID, ref, HEAD и ancestry;
  обнаружение неполных и конфликтующих публикаций.
- [x] Проверка bundles в отдельном quarantine и постоянный verified cache.
- [x] Fetch в отдельные tracking refs и pull только fast-forward с чистым worktree.
- [x] Ручной bundle GC всех веток: dry run, проверка сохраняемой истории с нуля,
  повторная сверка remote перед удалением; запрет при незавершённых CI-заданиях.
- [x] Собственные настройки и локальные данные в .gdi; Git config не изменяется.
- [x] Local config v2 с подключением по умолчанию и remote default NAME.
- [x] Короткие gdi push/fetch/pull/log с текущей веткой и default connection;
  явный выбор подключения и ветки.
- [x] Вывод подключения, URL и выбранной ветки в push/fetch/pull/log.
- [x] Gdi status: ветка, HEAD, worktree, подключения и свежее состояние публикации,
  без скачивания bundles; changes present красным, clean зелёным;
  Nothing to push. при опубликованном HEAD, Already up to date. при pull без обновления HEAD.
- [x] Gdi log: история публикаций, новые первыми; лимит записей, JSON,
  HEAD/Publication ID, тип и размер bundle, тема локально известного commit.
- [x] Прогресс этапов и передачи bytes/общего размера, процента и скорости на stderr
  без профилирования; JSON stdout остаётся чистым. Native RC использует stats groups,
  CLI — JSON stats. Тайминги и подробности rclone требуют профилирования.
- [x] Опциональный JSONL-профиль всех обращений к rclone, этапов и общего времени.
- [x] Первый этап оптимизации: пакетное чтение manifests,
  загрузки без лишних mkdir, durable receipt доставки обычного notification.
- [x] Immutable uploads с отказом перезаписи отличающихся bytes,
  в том числе при совпадающих размере и mtime.
- [x] Отказ от совместимости с Git exchange v1/v2 и local config v1
  по согласованному решению.

### CI и worker

- [x] Позиция CI follow сохраняется между вызовами wait/logs; проверяется identity
  запроса и неизменность выведенного prefix chunks. --restart воспроизводит лог
  с начала, обычный logs/output сохраняет полный лог; параллельные followers блокируются.
- [x] Проверки сбоев до/после ledger transitions, запуска/фиксации процесса,
  файлового fsync/replace и fsync каталога, финализации результата,
  metadata, каждого artifact и потерянных acknowledgements.
  Повтор доставки не исполняет CI заново; неопределённый запуск — INTERRUPTED.
  Atomic writes удаляют временные файлы и при ошибке replace/fsync.

- [x] Общая immutable inbox проектов и worker config без списка репозиториев.
- [x] GitHub Actions workflows из точного commit через act/Docker;
  workflow/event/job/inputs закрепляются в запросе.
- [x] GitHub namespace из origin без передачи URL и credentials.
- [x] Push --ci и ci submit/status/wait/logs/retry, JSON для автоматизации.
- [x] Durable outbox, повтор доставки с тем же job ID,
  явный retry завершённого задания с новым ID.
- [x] Постоянный worker, последовательное исполнение,
  foreground и systemd user service.
- [x] Worker check/run/install/start/status/stop;
  успешные start/stop не печатают пустой JSON-ответ.
- [x] Изолированный checkout точного SHA; пользовательский worktree не изменяется.
- [x] Worker cache и восстановление нужного commit после bundle GC.
- [x] Runtime-проверка act/Docker/images и execution revision,
  привязанная к фактическому окружению.
- [x] Таймауты, остановка process groups и очистка контейнеров конкретного job.
- [x] Проверка исходников после CI, required artifacts и защита от symlink escape.
- [x] Live console, heartbeat, immutable log chunks/events и полный binary log.
- [x] Проверка результата и artifacts, сверка chunks с полным логом до принятия PASS.
- [x] Pull --passed применяет ровно SHA выбранного проверенного PASS, ff-only.
- [x] SQLite ledger, persistent spool, atomic writes/fsync,
  восстановление доставки без повторного исполнения CI.
- [x] Неопределённое исполнение отмечается INTERRUPTED;
  чужие claims не перехватываются автоматически.

### Документация

- [x] Инструкции установки, worker/service/recovery, протоколы,
  краткий справочник и инструкция автономному агенту.
- [x] Документация самостоятельного проекта с абстрактными примерами;
  пути к исходникам обозначаются INSTALL_DIR.
- [x] Правило согласования будущих изменений обратной совместимости в AGENTS.md.
