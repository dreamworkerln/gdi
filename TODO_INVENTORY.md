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
| [TODO_004](todos/TODO_004_WORK_CHECKPOINT_IMPORT_RETENTION.md) | Work checkpoints, squash import, GDI-IMPORTED и retention | IMPLEMENTED; work/import/CI/archive/retention code и docs готовы; 435df3c опубликован (399 + 2 Docker PASS); все 6 remote активированы; новая правка --init default готова, tests NOT RUN |

## Следующие действия

1. TODO_004: runtime и agent.md из 435df3c опубликованы; lifecycle включён только
   на всех 6 remote root gdrive:gdi, worker перезапущен. После этого по указанию оператора новые пустые
   remote создаются с work_version=1 сразу. Эта новая правка и regressions готовы,
   tests NOT RUN; commit/push/deploy этой правки ещё не выполнялись. Предыдущие 401
   software checks относятся к 435df3c, не к новому diff. Evidence — в TODO_004.
2. Остальные открытые темы TODO_001–003 сохранены; их исполнительный приоритет
   относительно новой работы оператор пока не назначил. После выбора обновить
   порядок здесь, не перенумеровывая задачи.

Следующий новый тематический ID: **TODO_005**. Work-ID и CI job ID имеют отдельные
пространства имён. Commit/push/deploy/test при миграции не выполнялись. После поручения на реализацию
source/tests/docs TODO_004 изменяются; runtime 435df3c опубликован, новая правка --init пока локальная.
