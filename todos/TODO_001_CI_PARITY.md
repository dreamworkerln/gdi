# TODO_001 — Единый полный CI локально и на GitHub

Status: OPEN / MIGRATED.
Implementation: NOT STARTED по исходной записи; на текущем HEAD заново не проверено.
Validation: NOT RUN при миграции.
Дата регистрации ID: 2026-10-21. Исходный запрос: 2026-10-16.
Checkpoint миграции: `20c6359de147a0dc66f542d3a467a24685020a39` (до правок документации).
Источник: [исходный TODO](history/LEGACY_TODO_20261021.md), одноимённый раздел.

## Исходный контракт


Запрос оператора 2026-10-16: локально и на сервере должны исполняться одинаковые
проверки из одного общего конфигурационного источника/исполнителя. Finding
checkpoint: `dev@1ae1cb11465848567a39f74cc0fc2e05a99ba5b4`, clean до записи задачи.
Выполнена проверка конфигурации; тесты и Docker/Drive integration не запускались.

Наблюдения:

* GDI worker уже исполняет `.github/workflows` проверяемого commit через act;
  это общая workflow-конфигурация для настоящего GDI CI и GitHub.
* Локальный `bash test.sh` вызывает только `unittest discover`, подхватывая act
  из PATH. GitHub workflow отдельно задаёт `GDI_TEST_ACT`, устанавливает act
  v0.2.89 и rclone v1.75.1, затем выполняет discovery и отдельный Docker job.
* `DockerV2CliTests` локально skip без `GDI_TEST_DOCKER` и `GDI_TEST_ACT`, тогда
  как на GitHub этот набор обязателен в `docker-acceptance`. Act/rclone fixtures
  тоже зависят от env/tools. Поэтому успешный обычный test.sh не равен full CI.
* Проверки реального Drive и установленной службы — отдельные явно разрешаемые
  acceptance scenarios; их нельзя автоматически включать в обычный software CI.

Задача:

- [ ] Определить один entrypoint полного CI для local/GitHub с единым планом,
  аргументами, необходимыми версиями tools, feature flags и PASS/FAIL policy.
  Использовать существующий workflow через act либо общий script, вызываемый
  workflow; не заводить второй независимый список тестов.
- [ ] Включить одинаковую обязательную Docker/act/rclone acceptance часть
  полного software CI в обоих окружениях. Недостающие обязательные tools/image
  должны давать явный отказ подготовки, а не скрытый skip и ложный full PASS.
- [ ] Сохранить возможность быстрых subset runs и явно отличать их результат
  от полного CI; привести test.sh, README и docs/acceptance.md к одному контракту.
- [ ] Проверки Drive/service оставлять отдельными opt-in scenarios с явными
  условиями, scope и результатом; не обращаться к пользовательскому Drive/service
  автоматически при обычном запуске тестов.
- [ ] Добавить тесты паритета общего плана/workflow, env/preflight и обязательных
  suites; отрицательные cases должны обнаруживать случайное исключение Docker
  или act tests из одного варианта полного CI.
- [ ] Выполнить полные local/GitHub-compatible проверки на одной exact revision;
  зафиксировать реально выполненные и skipped cases, tools/image identities и
  ограничения. Обновить документацию без изменения exchange/worker semantics.

Критерий закрытия: одна согласованная команда полного CI гарантирует одинаковый
обязательный набор local/GitHub; обязательные integration tests не теряются из-за
разного окружения. Targeted/host acceptance не выдаются за полный software PASS.
Implementation: NOT STARTED. Validation: NOT RUN. Commit/push/deploy: NOT RUN.

## Границы и следующие действия

Обмен Git, scheduling и worker semantics не менять в рамках паритета CI.
Перед реализацией сверить сохранённые наблюдения с выбранным HEAD. После реализации
зафиксировать exact revision, обязательные checks/skips и evidence. Миграция не
является тестовым прогоном или подтверждением актуальности finding на текущем HEAD.
Связанные документы: [acceptance](../docs/acceptance.md),
[политика TODO](../TODO_MANAGEMENT_POLICY.md).
