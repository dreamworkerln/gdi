# TODO_013 — Ограничение запросов и постоянная диагностика rclone

Дата: 2026-11-13. Источник: два сообщённых оператором сбоя доступа к Google
при работе GDI; причинная связь с нагрузкой не доказана.
База: dev@84d3a816518ac4eb31bf917fa2ad3ade5ec661e2; рабочая правка без commit.
Status IMPLEMENTED; Implementation DONE локально; Validation PASS локального полного прогона: 539 tests, 534 PASS / 5 opt-in SKIP.

## Согласованный контракт

Изменить CLI/worker, без изменения Git protocol, IDs и данных Drive.
Агентские offline-команды и обращения Drive-коннектора не лимитируются.
По умолчанию rclone tpslimit=1, burst=1 на процесс, DEBUG без HTTP dumps.
Worker лог: XDG_STATE_HOME/gdi/logs/worker-rclone.log, 5 файлов по 10 MiB.
CLI: cli-rclone-UTC-PID.log, каждый запуск до 2 файлов по 5 MiB;
при следующем CLI запуске очистка старше 7 дней и бюджета 90 MiB до нового лога.
Параллельные CLI могут временно превысить совокупные 100 MiB; это не дисковая квота.
Каталог 0700, новые файлы 0600. Логи локальные; DEBUG может содержать пути.
HTTP dumps не включаются, userinfo прокси редактируется.
Ошибка транспорта указывает путь к логу. RC пишет длительности и номера операций.

Ошибки inbox: пауза 120, 240, 480, 900 s на имя; recovery и inbox не повторяют
один пакет в одном цикле. Паузы в памяти, после рестарта начинаются заново.
Общие ошибки цикла: отдельный потолок не ниже 900 s, независимо от idle max.
Существующие явно заданные retries сохраняются; default остаётся 1.
Общего межпроцессного лимита CLI+worker нет.

## Проверка и оставшиеся gates

- Реализация проверена чтением diff, синтаксическим разбором и полным набором тестов.
- Добавлены 7 regression tests: права/ротация, retention/redaction, активный CLI,
  CLI flags/errors, RC startup flags/persistent stderr, inbox recovery/backoff,
  общий backoff при idle cap=60. Все семь включены в успешный полный прогон.
- Реальный Drive acceptance и независимый review остаются открыты.
- Проверить ротацию, параллельные CLI, DEBUG/ошибки, лимиты и recovery fake transport.
- CLI использует editable checkout и видит изменения; worker config не редактировался, runtime не опубликован.
- Оператор явно попросил остановить службу: systemctl stop завершился успешно,
  последующее is-active показало inactive. Позже оператор явно разрешил запуск worker.

Критерии приёмки: лимит передаётся обоими транспортами; задержки сохраняют пакет;
нет второй обработки recovery в одном цикле; логи ограничены и доступны после
закрытия RC, CLI-ошибки остаются информативны, offline agent не запускает rclone.

Статические проверки: ast.parse изменённых Python-файлов и git diff --check PASS.
Это не выполнение regression tests и не проверка реального rclone/Drive.

## Проверка после разрешения оператора

Полный `test.sh`: 539 tests за 336.873 s; failures=3, errors=3, skipped=5,
exit 1. Лог /tmp/gdi-tests-20261113-aFTDGJ/output.log. Sandbox escalation был
запрошен сразу; ошибок ограничений песочницы в отчёте нет.

Исправления только тестов: три сценария восстановления CI/inbox теперь проверяют
сохранение задержки до deadline и успешный повтор после неё; missing executable
перехватывает Popen; mock потока сохраняет name для logging; timeout during GET
изолирован от pacing (`tpslimit=0` только в этом тесте).

Разрешённый targeted прогон с require_escalated: 7 новых и 5 затронутых тестов,
12 tests за 3.859 s, OK, exit 0. Лог:
/tmp/gdi-tests-20261113-aFTDGJ/fixed-tests.log.
Первоначально полный повтор оставался открыт; результат последующего прогона ниже.

## Полный повтор и разрешение commit/start

Повторный `./test.sh` с require_escalated завершился: 539 tests за 346.441 s,
OK (skipped=5), exit 0 — 534 PASS, пять opt-in SKIP.
Лог: /tmp/gdi-tests-full-20261113-pkejv2/output.log;
exit-code: /tmp/gdi-tests-full-20261113-pkejv2/exit-code.
Исполняемые исходники и тесты после запуска прогона не менялись.
Оператор подтвердил результат и явно поручил commit всех изменений и запуск
службы worker. Публикацию выполняет сам: push/deploy/runtime не разрешены.
Status остаётся IMPLEMENTED до отдельного real Drive acceptance.
