# TODO_014 — Синхронный локальный worker barrier перед fetch/pull

Дата: 2026-11-13. Источник: оператор попросил перед локальными fetch/pull
разбудить worker и ждать обработки входящих агентских Git-push; отдельно
подтвердил общий mutex с таймерным обходом inbox. Push не будит worker.
База: 47b64aa. Status IMPLEMENTED; Implementation DONE локально;
Validation PASS локального полного прогона: 563 tests, 558 PASS / 5 opt-in SKIP; реальная служба не перезапускалась.

## Контракт

CLI до git.lock ищет локальные Unix sockets по inbox_root. Если слушателя нет,
сохраняется обычная работа. Подключённый worker обязан подтвердить barrier:
ошибка, backoff, таймаут 300 s прекращают fetch/pull, молчаливого stale read нет.
Сокеты в XDG_RUNTIME_DIR/gdi-control-UID (fallback /tmp/gdi-control-UID),
каталог 0700, сокет 0600. Каждый worker имеет отдельное имя по root/worker ID.
Старый worker без endpoint не обнаруживается: для активации нужен явный restart.
Протокол/config/Repository IDs/Git SHA не меняются.

Сервис работает отдельным потоком и частным TransportSession: только выбранный
repository ID под inbox_root, не запускает CI, не использует ledger. CLI ждёт
recovery и обработки адресованных этому worker Git-пакетов, включая pointer и
receipt. Общий inbox_lock охватывает весь normal discover_inbox/recover и весь
локальный scan/adoption. Одновременные обращения к одному remote объединяются,
ошибки возвращаются всем ожидающим. Все сетевые ограничения и backoff сохраняются.
Ответ относится к снимку inbox при начале обхода; более поздние загрузки могут
потребовать следующего fetch. Запросы к нескольким локальным worker идут по очереди.

## Проверки / acceptance

Добавлены 24 теста в test_worker_control.py (12) и
test_worker_control_packets.py (12), все включены в успешный полный прогон:
- отсутствующий/старый endpoint, несколько worker, синхронное ожидание/ошибка;
- coalescing двух одновременных CLI в один проход;
- таймаут клиента, shutdown дожидается активного handler, cleanup socket;
- права/длина path, unsafe directory, malformed requests/responses;
- lifecycle endpoint только для continuous shared worker;
- mutex в обоих порядках: timer блокирует local, local блокирует timer;
- реальный пакет перемещается ровно один раз при concurrent timer/local;
- pointer/receipt готовы до ответа; ZIP не загружается повторно;
- фильтрация repo/worker/CI, recovery после move, backoff и последующий успех;
- конфликты до изменений, неверный route до публикации;
- реальный CLI fetch/pull ждёт до git.lock и получает новый commit;
- ошибка barrier запрещает чтение remote; push не вызывает barrier;
- запрос проходит, пока реальный tick удерживается в process CI (без запуска CI).

Используются Unix sockets, реальные локальные Git-пакеты и MemoryTransport;
Google Drive не вызывается, rclone не запускается. Предыдущие fixtures
Worker.__new__ адаптированы к inbox_lock/shared lifecycle.
Статические ast.parse и git diff --check PASS; targeted результат ниже.
Оператор разрешил targeted и полный прогоны, затем commit всех изменений.
Real service/Drive acceptance, deploy и service restart в этой задаче не выполнялись.

## Разрешённая targeted validation

По запросу оператора запущены с require_escalated:
python3 -m unittest -v tests.test_worker_control tests.test_worker_control_packets
 tests.test_rclone_policy tests.test_packet_v4
Результат: 53 tests за 27.612 s, OK, exit 0. Все 24 новых теста PASS.
Лог: /tmp/gdi-worker-control-tests-20261113.log;
код: /tmp/gdi-worker-control-tests-20261113.exit-code.
Проверенная база 47b64aa плюс текущая рабочая правка TODO_014, без source edits
во время прогона. Это subset, не полный PASS. Реальная служба не перезапускалась.

## Полный прогон и commit

По отдельному запросу оператора выполнен ./test.sh с require_escalated,
без наблюдения: 563 tests за 380.054 s, OK (skipped=5), exit 0.
Итого 558 PASS / 5 opt-in SKIP.
Лог /tmp/gdi-full-barrier-20261113-WsM8a8/output.log;
код /tmp/gdi-full-barrier-20261113-WsM8a8/exit-code.
Оператор подтвердил результат и запросил git commit. Исполняемые исходники и
тесты после запуска прогона не менялись; обновлена только запись validation.
Проверенный checkpoint: база 47b64aa плюс source/test changes текущего commit.
Status IMPLEMENTED сохраняется до real service/Drive acceptance.
Публикация и restart этим commit не выполняются. git diff --check PASS.

## Расширение: status / s (2026-11-13)

Оператор отдельно поручил добавить тот же barrier перед status и тесты.
Реализовано до git.lock и до любых remote status reads: выбранный remote либо
все подключения без аргумента. Дедупликация по inbox_root/URL/Repository ID.
Ошибка любого barrier прерывает status целиком; локальные refs не меняются.
Стандартный alias s разворачивается в status существующим механизмом.

Добавлены 9 тестов tests/test_status_worker_control.py: реальный Git packet и
новый published head до ответа без изменения checkout/refs; alias через main;
выбранный remote; все barrier до чтений; одинаковые подключения; error/backoff/
timeout; ошибка второго подключения без частичного успеха; неизвестный remote;
пустой список подключений. Все девять включены в успешный полный прогон.
ast.parse и git diff --check PASS. Validation PASS полного локального набора:
572 tests за 410.380 s, 567 PASS / 5 opt-in SKIP, exit 0.
Команда ./test.sh с require_escalated по запросу оператора.
Лог: /tmp/gdi-full-status-20261113-SDxe8u/output.log;
код: /tmp/gdi-full-status-20261113-SDxe8u/exit-code.
Checkpoint: база f8e5480d4133eabc0c84f83a24e56d5baabe0b1f плюс source/test patch
текущего commit. Исходники и тесты после старта прогона не менялись.
Оператор подтвердил результат и поручил commit всех изменений.
Restart/deploy для расширения не выполнялись; real service/Drive acceptance открыт.
