# TODO_010 — Однофайловый обмен агента с минимумом вызовов коннектора

Дата: 2026-11-12. Источник: оператор сообщил о 11 минутах agent push и явно
разрешил несовместимый новый агентский формат с обновлением CLI/worker/runtime.
База: dev@68e49df0d851fd8e1d7c6e7838f210ae06b6e33a.

Status: IMPLEMENTED. Implementation: DONE. Validation: PASS локального test.sh
(507 tests, 502 PASS / 5 opt-in SKIP); service/реальный Drive acceptance открыты.
На момент завершения локальной валидации commit, deploy, Drive и перезапуск
рабочего GDI worker не выполнялись. Затем оператор явно разрешил Git commit;
публикация и обновление службы этим запросом не разрешены.

## Проблема и согласованный контракт

Коннекторные list/fetch/upload дороги по времени. Убрать повторные сетевые
проверки, snapshots и ручную публикацию checkpoint агентом. Agent pull скачивает
один готовый export ZIP; agent push загружает один ZIP и получает одну квитанцию.
Первичное обнаружение Drive ID учитывается отдельно. Worker выполняет native
публикацию и checkpoint. CLI, worker и runtime обновляются вместе; старый агентский
путь не является fallback по умолчанию. Пользователь отдельно попросил новую
каноническую инструкцию для агента.

Native protocol v3, Repository ID и SHA сохраняются. Нет автоматических веток,
squash, Work-ID, CI, миграции remote или внешних изменений в рамках реализации.
Полный bundle сознательно выбран для независимости одного пакета от базы/cache.
Сетевые проверки raw store остаются внутри host GDI, не в коннекторном workflow.

## Реализация и приёмка

- [x] Offline agent pull/push, строгий ZIP, saved base/plan и квитанции.
- [x] Host export при native push; worker routing и inbox publication без CI.
- [x] Durable journal/retry, expected-base конфликт и GC guard.
- [x] Новая docs/agent.md; специальный справочник внутри zipapp без нового fetch.
- [x] Локальный просмотр кода и регрессионные сценарии (17 новых tests).
- [x] Полный локальный test.sh: 507 tests, 502 PASS / 5 opt-in SKIP, exit 0.
- [x] Подготовить все изменения к Git commit по запросу оператора 2026-11-12.
- [ ] По отдельному разрешению: публикация и обновление всех компонентов.
- [ ] Отдельная реальная проверка Drive ID/update semantics и стоимости вызовов.

Негативные acceptance: повреждённый/чужой пакет; грязная или другая ветка;
divergence; stale base; повтор request после утраты ack; сбой после publication
до journal; маршрутизация другому worker; отсутствие worker; GC с pending inbox.
Проверить отсутствие CI и изменения пользовательского checkout при publication.

## Evidence и оставшиеся gates

Исходная рабочая правка поверх указанной базы до исправления ZIP handler:
`/tmp/gdi-agent-exchange-20261112/source.patch` (tracked diff + все новые файлы).
Статический просмотр: `git diff --check` PASS; `ast.parse` для 52 Python-файлов
(все gdi/*.py, deploy и затронутые tests) PASS. Это не запуск тестов или runtime.
Исторические 490 tests TODO_009 не проверяют этот новый код. Инструкция подготовлена
в исходниках, но не опубликована в runtime. Локальный синтаксический просмотр не
заменяет функциональную приёмку.

2026-11-12: оператор разрешил полный `./test.sh` с повышенными привилегиями,
без наблюдения. Первый завершённый прогон: 505 tests, 496 PASS, 4 FAIL, 5 opt-in
SKIP; 506.949 s, exit 1. Лог: `/tmp/gdi-tests-20261112-KC5Ku7/output.log`.
Все четыре FAIL — новые негативные сценарии: собственный GdiError (подкласс
RuntimeError) попадал в общий ZIP handler, включая исключения вызывающего кода
после yield, и терял исходную диагностику.

По отдельному запросу оператора исправлено: ZIP handler ограничен декодированием
архива, GdiError пропускается без замены, verify_bundle и yield вынесены за этот
handler. Добавлены проверки сохранения consumer RuntimeError и адаптации ошибки
самого ZIP reader. Ожидания четырёх упавших тестов не ослаблялись.

Повторный полный запуск: `./test.sh` с elevated sandbox, без systemd и наблюдения.
Каталог `/tmp/gdi-tests-repeat-20261112-tMTGdQ/`: output.log, exit-code после
завершения, source.patch и его SHA256 для точных исходников повтора.
Повтор завершён: 507 tests, 501 PASS / 1 FAIL / 5 opt-in SKIP, 478.875 s, exit 1.
Все 17 новых test_agent_sync PASS, включая четыре ранее упавших сценария.
Оставшийся FAIL: RealInboxTests.test_real_rclone_and_act_fail_fix_pass, строка 597.
Act при второй CI-операции не смог слушать 127.0.0.1:41919: address already in use.
Тест заранее выбирает порт через bind(..., 0), затем закрывает сокет до запуска
worker/act. Источник фактической занятости порта по этому логу не установлен;
этот конкретный прогон не считается PASS.

Следующий явно разрешённый запуск без дополнительных изменений исполняемого кода:
`./test.sh` с elevated sandbox, без systemd и наблюдения. Лог:
`/tmp/gdi-tests-repeat2-20261112-DVZfcB/output.log`, exit-code: 0.
Итог: 507 tests за 378.432 s, OK (skipped=5): 502 PASS / 5 opt-in SKIP.
Исполняемые исходники соответствуют сохранённому source.patch предыдущего повтора;
между запусками обновлялась только запись результатов в TODO/реестре.
RealInboxTests в этом запуске PASS. Конфликт порта не воспроизвёлся, отдельное
исправление выбора порта не вносилось. Пропущенные Docker/service сценарии и
реальные Drive ID/update semantics остаются открытыми gates.
