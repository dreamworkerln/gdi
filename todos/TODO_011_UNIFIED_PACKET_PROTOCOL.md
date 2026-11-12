# TODO_011 — Общий пакет и минимальный Drive I/O для всех клиентов

Дата 2026-11-12; источник — операторский лог push -f после TODO_010.
База dev@04c01e5f83d87bb9fd85db0b549dc14aa414ee80; исходное дерево чистое.
Status IMPLEMENTED; Implementation DONE; Validation PASS локального test.sh —
528 tests: 523 PASS / 5 opt-in SKIP; real Drive/service acceptance открыты.

Оператор разрешил несовместимый protocol v4 для CLI/агента/worker и новые
инструкции после deploy. Затем явно разрешил добавить tests и запустить весь
test.sh без наблюдения. После успешного прогона оператор отдельно разрешил Git
commit всех изменений. Публикация, изменение службы и миграция Drive не выполнялись.

## Контракт

- Один полный ZIP (Git bundle + metadata) на публикацию; один маленький head JSON.
- Native push: одно начальное чтение head, один upload ZIP, один update head.
  При отсутствии head — одно чтение repository.json для первого доверенного push.
- Нет повторных цепочек manifests, обратных скачиваний, archive/checkpoint upload,
  inline GC или repository_updated notification при обычном push.
- Agent pull: head + выбранный пакет. Agent push: один inbox ZIP + квитанция.
  Worker перемещает тот же ZIP серверной операцией, не пересобирает/не загружает.
- Проверки содержимого и FF локальны; сетевые ошибки транспорта не скрывать.
- Publication ID — digest единственного ZIP; Git SHA/Repository ID сохраняются.
- Старые пакеты не удаляются при push; отдельный GC при остановленных писателях.
- Повторы/частичные ошибки сохраняют план. Recovery может делать адресные чтения,
  но не добавляет проверочных циклов в успешный путь.
- Нет обещания CAS: несколько независимых писателей одной ветки сериализуются
  оператором. Предыдущее многократное чтение также не обеспечивало транзакцию.

## План и gates

- [x] Формат, native push/fetch/clone/log/status, explicit upgrade с сохранением IDs.
- [x] Agent pull/push и worker reuse/recovery.
- [x] CI/work/GC интеграции; убрать скрытые повторные Git-публикации.
- [x] Новая инструкция и runtime, регрессионные сценарии и бюджеты обращений.
- [x] Разрешённый полный прогон test.sh с elevated sandbox: 523 PASS / 5 SKIP.
- [x] Подготовить все изменения для запрошенного оператором Git commit в dev.
- [ ] По отдельному разрешению deploy, миграция и real Drive acceptance.

Существующие bundles, manifests и CI данные на Drive автоматически не удалять.
Legacy reader нужен только для явного upgrade/исторического CI и tests v3;
обычный операторский push не должен тихо возвращаться к дорогому протоколу.

## Реализация и область проверки

Новые тесты test_packet_v4 (22 сценария) используют текущий Exchange и отдельный transport spy:
число чтений/записей, отсутствие exports/notifications/read-back, retry после
потери ответа/ошибки pointer, один пакет для agent/CLI/CI, worker move/recovery,
upgrade/clone, corruption/FF, GC и work import/retirement.
Старые tests с layout v3 явно используют LegacyExchange и сохраняются как
исторические/upgrade regression tests; это не fallback обычных команд v4.
Runtime/deploy tests переведены на v4, включая запрет обратного download runtime.
Есть отдельный реальный rclone local-backend сценарий CLI и RC: один пакет
загружается в inbox и перемещается worker без пересборки/повторного upload.

Локальный статический просмотр: ast.parse всех gdi/tests/scripts Python-файлов и
git diff --check. Functional PASS до завершения полного прогона не заявляется.
Предыдущие 502 PASS / 5 SKIP относятся к 04c01e5, не к новой реализации.

Полный `./test.sh` запущен по разрешению оператора с require_escalated, обычным
процессом без systemd. Каталог `/tmp/gdi-v4-tests-20261112-RTXZf3/` содержит
output.log, source.patch и source.patch.sha256; exit-code появится после завершения.
Снимок — BASE 04c01e5 + все tracked/untracked изменения до запуска; эта последняя
запись о запуске и статус в реестре не меняют исполняемые исходники снимка.
Прогон проверен по сообщению оператора об ошибках: 528 tests за 388.307 s,
516 PASS / 3 FAIL / 4 ERROR / 5 opt-in SKIP, exit 1. Все 22 новых v4 сценария,
включая реальный rclone CLI/RC move, PASS. Полный PASS не заявляется.

Исправления по tracebacks: исторический agent.clone явно использует LegacyExchange
и отвергает v4 snapshot (для v4 используется agent pull). Два настоящих CLI
roundtrip/GC теста переведены с ожиданий incremental/branches/bundles на full ZIP,
heads/packets; проверки Git SHA, сохранности указателя, реального pull и повторного
GC остаются. Тест read_optional отражает согласованное отсутствие stat: проверяет
один cat и локальный отказ metadata parser на пустом содержимом, не требует
предварительного запроса типа объекта. Новых Drive read-back не добавлено.

Повторный полный `./test.sh` запущен с require_escalated, без systemd/наблюдения:
`/tmp/gdi-v4-tests-repeat-20261112-Fy4olE/output.log`. Рядом сохранены source.patch
и source.patch.sha256 (BASE 04c01e5 + рабочая правка перед запуском); exit-code
появится после завершения. Эта запись обновлена после старта, код не менялся.
Результат повтора проверен после сообщения оператора: 528 tests за 391.008 s,
OK (skipped=5), exit-code 0: 523 PASS / 5 opt-in SKIP. Исходники соответствуют
снимку source.patch перед этим запуском, после старта менялись только записи
результатов в TODO/реестре. Пропуски Docker/service не считаются проверенными.
Реальный Google Drive и миграция пользовательских remote пока не проверялись.
