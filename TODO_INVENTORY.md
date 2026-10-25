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
| [TODO_006](todos/TODO_006_CI_RESULT_PACKAGE.md) | Один пакет результата CI, без log chunks | IMPLEMENTED; новый формат согласован, чтение старых результатов сохранено; полный test.sh: 441 PASS / 5 opt-in SKIP; разрешённое обновление CLI/worker/runtime выполняется |

## Следующие действия

0. TODO_006: завершить проверки и обновление CLI, worker и runtime/agent.md по явному
   запросу оператора 2026-10-25. Доставка результата — один пакет без chunks.
1. TODO_004: runtime/agent.md 435df3c были опубликованы отдельно; default capability
   новых remote включена в опубликованный b5886b0, теперь создание выполняет
   `remote create`. 2026-10-24 оператор снял work_version со всех шести существующих
   remote для прежнего клиента; повторная активация и новый runtime deploy не
   выполняются автоматически. История и открытые service/real Drive gates — в задаче.
2. Остальные открытые темы TODO_001–003 сохранены; их исполнительный приоритет
   оператор пока не назначил. После выбора обновить порядок здесь.

Следующий новый тематический ID: **TODO_007**. Work-ID и CI job ID имеют отдельные
пространства имён. Исторические сведения о миграции и прежних публикациях сохранены
в тематических задачах; они не означают проверки или активации текущих remote.
