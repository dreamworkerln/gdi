# TODO_006 — Пакет результата CI без log chunks

Status: CLOSED; software validation и запрошенное обновление выполнены.
Implementation: DONE.
Validation: PASS — bash test.sh, 446 tests: 441 PASS, 5 opt-in SKIP.
Оператор отдельно разрешил тесты перед обновлением.
Дата: 2026-10-25. Исходный checkpoint: `e761c4dfbd464aec3668b7165129225f176a8c25`.
В рабочем дереве до задачи уже изменён default `CHECKPOINT_EVERY` с 10 на 5
и соответствующие описания; эти изменения сохраняются отдельно от CI-контракта.
Источник: обсуждение оператором отчёта агента о 21–22 минутах работы при
6 минутах 30 секундах исполнения CI; явный запрос убрать chunks и оставить
heartbeat/status.json во время исполнения, один пакет после завершения.

## Цель и согласованные решения

- Сократить число downloads через Drive-коннектор; параллельные downloads могут
  быть недоступны. Более крупный файл предпочтительнее множества мелких.
- Во время исполнения публиковать mutable status.json с heartbeat и stage.
- После исполнения публиковать один immutable архив с result.json,
  final-status.json, полным binary build.log и объявленными artifacts.
- Не создавать и не публиковать новые log chunks. Не требовать их для проверки
  нового результата. Лог и artifacts проверять по SHA256 и размерам результата.
- Сохранить identity job/repository/publication/HEAD/profile/revision/run/request,
  PASS gates, durable cancellation и отдельную доставку поздней отмены.
- Не запускать CI повторно из-за ошибки упаковки или доставки.
- Git exchange v3, локальная конфигурация v2 и repository IDs не меняются.

## Совместимость и обновление — согласовано

2026-10-25 оператор явно ответил: «Да, новый формат; все клиенты обновить».
Разрешена несовместимость со старыми клиентами; чтение исторических результатов
сохранено. Затем оператор поручил «все обновляй» и отдельно разрешил сначала
провести тесты после исправления кода. Это разрешение на обновление CLI, worker
и runtime/agent.md; Git history и repository IDs сохраняются, старые результаты
не переписываются и не удаляются. Другие будущие несовместимости не разрешены.

## Реализованная доставка

Один content-addressed файл `result-<sha256>.zip` в папке job либо `cancelled/`.
SHA256 имени проверяется по bytes архива; внутри — точный result.json и файлы
его descriptors. Дополнительный remote descriptor скачивать не требуется.
Полный listing папки доказывает отсутствие/единственность итогового пакета.
Дубликаты/несколько пакетов, неверный hash, пропущенные/лишние/дублирующиеся
ZIP entries и несовпадающие identity/размеры/hashes должны приводить к отказу.
Пакет собирается из durable результата и публикуется после исполнения;
read-back проверяет пакет целиком перед ledger PUBLISHED и удалением inbox.
Отмена повторно проверяется до/после upload и проверки пакета.

## План и критерии приёмки

- [x] Согласовать совместимость и зафиксировать решение здесь и в реестре.
- [x] Реализовать детерминированную упаковку и потоковую проверку/распаковку,
  включая binary/empty log и крупные artifacts без загрузки всего архива в RAM.
- [x] Перевести worker и read-back/recovery/cancellation на пакет; никаких
  новых remote log-chunks/events/отдельных artifacts/full log.
- [x] Перевести общий reader и offline agent snapshot на один пакет.
- [x] Обновить wait/logs: heartbeat/stage во время работы, полный проверенный
  лог после завершения; ожидание/таймаут клиента не отменяют job.
- [x] Адаптировать проверки: полный CLI/offline цикл, corrupt/missing/extra ZIP
  entries и hash/identity, interrupted upload/read-back, restart без rerun,
  поздняя отмена, бинарный лог, повторный просмотр результата.
- [x] Обновить docs/agent.md, CI protocol, CLI/worker/архитектуру и справочники.
- [x] По отдельному разрешению выполнить проверки и сохранить exact evidence.

## Выполненные проверки и оставшиеся gates

Окончательный полный прогон: `bash test.sh` — **OK, 446 tests, 441 PASS,
5 SKIP**, 438.962 s. Пропущены 2 opt-in Docker и 3 opt-in user-service cases.
Команда запускалась вне ограничения sandbox на Unix-сокеты: local rclone RC
является частью integration coverage. Пользовательские CI/workflows и hardware
не запускались. Сырой лог: [full-tests.txt](evidence/TODO_006/full-tests.txt).
Исходники во время окончательного прогона не менялись.

Checkpoint: BASE `e761c4dfbd464aec3668b7165129225f176a8c25` +
[source.patch.gz](evidence/TODO_006/source.patch.gz), SHA256 распакованного patch
`4f04f5082a3a8fe15a634bc00aa648100210a2c1adcb3d36de0ae77bf6c87a24`.
[Source manifest](evidence/TODO_006/source-manifest.json) содержит hashes всех
проверяемых Python sources/tests и test.sh/deploy.py. Итоговые docs/evidence
добавлены после проверки исходников.

Предшествующие проверки: 83 targeted PASS; package/live status — 9 PASS;
follow — 7 PASS; отдельный concurrent cold-cache case — PASS. Полный итоговый
прогон включает новые проверки одного download, повреждения/кеша, concurrency,
поздней отмены, restart/recovery и сохранённого legacy чтения.

Исправленные случаи: после удаления chunks/events понадобился явный mkdir
cancelled/; общий cache download получил уникальный pending pathname для двух
одновременных клиентов. Первый полный прогон также выявил старую ошибку теста
init: второй git.snapshot сам обновлял .git/index перед побайтным сравнением.
Доказано отдельно: initialize не меняет index, snapshot меняет. Тест теперь
сначала сравнивает защищённые bytes после init, затем проверяет Git snapshot;
производственный код init не изменён.

## Публикация и обновление — выполнено

Оператор отдельно подтвердил публикацию проверенного commit на Drive, замену
runtime/agent.md и restart user-dream; затем явно поручил GitHub push.
Исходный commit: `7b47d820027acdd06a4a9017159c5ef3f4967448`, опубликован в GitHub
origin/dev и GDI dev. Полная GDI publication:
`478a3b1977d35871f4a7004eb5ab66e82192ec94009346b95fa1209288395f18`.

`python3 scripts/deploy.py`: сборка/smoke, full GDI publication, upload всех четырёх
runtime files и обратное сравнение bytes — PASS. Корневой deploy.sh не запускался;
GitHub push выполнен отдельно по прямому запросу оператора.
Runtime HEAD совпадает с исходным commit, 148660 bytes, SHA256:
`e23264c1f5d875ee3b57a6e6333e77c6635aeb84cc7bdba01f8dbed3d077ceaa`.
Папка: `gdrive:gdi/gdi/runtime/`; обновлены gdi.pyz/runtime.json/gdi.pyz.sha256/agent.md.
Дополнительный isolated zipapp smoke вызвал agent ci result из вне source tree
на полном offline-пакете: PASS, verified:true, точные bytes build.log совпали.
Сырой итог: [runtime-smoke.txt](evidence/TODO_006/runtime-smoke.txt).

Локальный CLI — editable из этого checkout, default checkpoint-every=5 подтверждён
справкой. Перед restart ledger user-dream не содержал pending jobs. После
`gdi worker restart`: active/running, ExecMainStatus=0, MainPID=380402,
ActiveEnterTimestamp=2026-10-25 20:03:54 MSK. Свежий read Drive capabilities
подтвердил worker_id=user-dream и result_delivery_version=1.
[Deployment record](evidence/TODO_006/deployment.json).

Другие hosts и уже работающие контейнеры агентов дистанционно не управляются
из этой сессии; агентам перед новым CI нужно получить опубликованный runtime
и актуальную agent.md. Исторические данные/remote IDs сохранены, remote migration
и пользовательские CI/hardware runs не выполнялись. Замер реального выигрыша
через пользовательский Drive-коннектор остаётся последующим наблюдением,
а не заявленным результатом этих software checks.
