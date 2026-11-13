# TODO inventory — GDI

Авторитетный реестр текущих инженерных задач. Правила:
[TODO_MANAGEMENT_POLICY.md](TODO_MANAGEMENT_POLICY.md).
Точка миграции 2026-10-21: `dev@20c6359de147a0dc66f542d3a467a24685020a39`, дерево до документационных правок чистое.
Открытые темы старого TODO перенесены без повторной ревизии кода и тестов;
[полный исторический снимок](todos/history/LEGACY_TODO_20261021.md) сохранён.
Исторические completed/review результаты не объявляются проверкой текущего HEAD.

## Задачи

| ID | Тема | Состояние |
| --- | --- | --- |
| [TODO_001](todos/TODO_001_CI_PARITY.md) | Общий полный local/GitHub CI | OPEN; историческое NOT STARTED, текущая реализация не перепроверена; validation при миграции NOT RUN |
| [TODO_002](todos/TODO_002_CI_RELIABILITY_RETENTION.md) | Надёжность CI, локальное/Drive хранение, GC и act | OPEN; шесть прежних открытых пунктов; implementation UNKNOWN, validation NOT RUN |
| [TODO_003](todos/TODO_003_DRIVE_DISCOVERY_NOTIFICATIONS.md) | Changes API и опциональные notifications | OPEN; два прежних открытых пункта; implementation UNKNOWN, validation NOT RUN |
| [TODO_004](todos/TODO_004_WORK_CHECKPOINT_IMPORT_RETENTION.md) | Work checkpoints, squash import, GDI-IMPORTED и retention | IMPLEMENTED; опубликованная реализация b5886b0 сохранена; 2026-10-24 оператор снял work_version со всех 6 remote; повторная активация не выполнялась |
| [TODO_005](todos/TODO_005_REPOSITORY_CLI.md) | Обычные init/clone/remote create | CLOSED; GDI 0.4.0, init/clone/remote create; BASE b5886b0 + source patch/evidence в задаче, 127 targeted PASS и полный discovery 429 PASS / 5 opt-in SKIP |
| [TODO_006](todos/TODO_006_CI_RESULT_PACKAGE.md) | Один пакет результата CI, без log chunks | CLOSED; 7b47d82, один пакет без новых chunks; test.sh: 441 PASS / 5 opt-in SKIP; GitHub/GDI/runtime опубликованы, user-dream перезапущен и объявляет delivery v1 |
| [TODO_007](todos/TODO_007_CI_SINGLE_SUBMISSION.md) | Один самодостаточный запрос CI в inbox | CLOSED; 3b5e7af, один inbox-файл; test.sh: 455 PASS / 5 opt-in SKIP; GitHub/GDI/runtime опубликованы, user-dream обновлён и объявляет submission v1 |
| [TODO_008](todos/TODO_008_AGENT_SNAPSHOT_BUILDER.md) | Сборщик агентского snapshot и точный отчёт проверок | IMPLEMENTED; 0.5.0 локально; software validation: 32 targeted, test.sh до version bump 473 PASS / 3 opt-in service SKIP; version parity PASS; публикация отложена оператором |
| [TODO_009](todos/TODO_009_PUBLICATION_CHECKPOINTS.md) | Ограниченная активная цепочка, checkpoints и архив manifests | IMPLEMENTED; implementation DONE локально; test.sh: 490 tests, 485 PASS / 5 opt-in SKIP, 325.931 s; Docker/service и реальный connector acceptance открыты; deploy/активация/рестарт не выполнялись |
| [TODO_010](todos/TODO_010_AGENT_SINGLE_EXCHANGE.md) | Один export для pull, один inbox ZIP для push и квитанция worker | IMPLEMENTED; ZIP handler исправлен; test.sh 507: 502 PASS / 5 opt-in SKIP, 378.432 s, exit 0; service/реальный Drive acceptance открыты, deploy не выполнялся |
| [TODO_011](todos/TODO_011_UNIFIED_PACKET_PROTOCOL.md) | Общий packet protocol v4 для CLI/агента/worker без read-back | IMPLEMENTED локально; test.sh 528: 523 PASS / 5 opt-in SKIP, 391.008 s, exit 0; Git commit разрешён оператором; real Drive/service acceptance открыты, deploy/миграция не выполнялись |
| [TODO_012](todos/TODO_012_PROXY_HEADER_TIMEOUT.md) | Header timeout и автоматический fallback на env proxy | IMPLEMENTED; test.sh 532: 527 PASS / 5 opt-in SKIP, 372.544 s, exit 0; commit разрешён оператором, real Drive acceptance открыт |
| [TODO_013](todos/TODO_013_RCLONE_PACING_LOGS.md) | Лимиты rclone, постоянные логи, inbox backoff | IMPLEMENTED; полный повтор 539 tests: 534 PASS / 5 opt-in SKIP, 346.441 s; commit и запуск worker разрешены, публикация оператором отдельно |

| [TODO_014](todos/TODO_014_LOCAL_FETCH_BARRIER.md) | Локальный barrier перед fetch/pull/status | IMPLEMENTED; status/s включены; полный прогон 572 tests: 567 PASS / 5 opt-in SKIP, 410.380 s; commit разрешён, требуется restart |

## Следующие действия

Текущий запрос — TODO_014: расширение ожидания Git inbox на status / s.
Добавлены 9 тестов. Полный прогон с расширением: 572 tests,
567 PASS / 5 opt-in SKIP, 410.380 s.
Оператор разрешил commit; restart и публикация в новой задаче не выполнялись.

Предыдущий запрос — TODO_013: ограничение нагрузки и локальная диагностика rclone.
После исправления тестов полный повтор: 539 tests, 534 PASS / 5 opt-in SKIP.
Оператор разрешил commit всех изменений и запуск worker; публикацию выполняет сам.
CLI использует editable checkout и уже видит изменения; runtime не публиковался.

Предыдущий запрос — TODO_012: восстановить переключение на env proxy при точной
ошибке response-header timeout из показанного оператором push. Полный прогон
прошёл, оператор запросил Git commit; публикация не выполнялась.

Текущий приоритет — TODO_011: минимум суммарного Drive I/O, включая операторский
push и worker. TODO_010 остаётся историей первой реализации и её локальных тестов.
Предыдущий приоритет оператора — TODO_010: минимум агентских вызовов Drive-коннектора
и новая инструкция. Разрешённый повторный полный прогон после исправления ZIP
handler завершился успешно; осталось 5 opt-in SKIP. Публикация требует отдельного
запроса.

1. TODO_004: runtime/agent.md 435df3c были опубликованы отдельно; default capability
   новых remote включена в опубликованный b5886b0, теперь создание выполняет
   `remote create`. 2026-10-24 оператор снял work_version со всех шести существующих
   remote для прежнего клиента; повторная активация и новый runtime deploy не
   выполняются автоматически. История и открытые service/real Drive gates — в задаче.
2. Остальные открытые темы TODO_001–003 сохранены; их исполнительный приоритет
   оператор пока не назначил. После выбора обновить порядок здесь.

Следующий новый тематический ID: **TODO_015**. Work-ID и CI job ID имеют отдельные
пространства имён. Исторические сведения о миграции и прежних публикациях сохранены
в тематических задачах; они не означают проверки или активации текущих remote.
