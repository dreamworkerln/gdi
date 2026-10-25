# TODO_007 — Один самодостаточный запрос CI в inbox

Status: CLOSED; software validation и согласованное локальное обновление выполнены.
Implementation: DONE.
Validation: PASS — bash test.sh, 460 tests: 455 PASS, 5 opt-in SKIP.
Дата: 2026-10-25. BASE: `9209e68e685c5944a98d1eadd27a84908960e7e5`.
Источник: отчёт агента о запуске lora-sack (9:49 всего, 2:35 на передачу CI)
и запрос оператора «ок, действуй» после предложения одного файла задания.

## Согласованный контракт

- Агент готовит один canonical JSON с полным CI request и маршрутом проекта.
  Имя в общей inbox содержит job ID и SHA256 точных bytes.
- Публикация этого файла является передачей и готовностью задания; отдельных
  uploads request.json/request.ready и промежуточного check не нужно.
- Agent workflow: prepare → upload одного inbox-файла → read-back proof → accept.
  Accept использует исходный проверенный snapshot и полный listing/download proof
  общей inbox; второй snapshot с созданной папкой job не требуется.
- Worker проверяет schema/hash/identity, фиксирует запрос и маршрут в ledger,
  архивирует один submission.json в папке job перед исполнением. Старый
  многофайловый shared CI не является поддерживаемым путём отправки.
- Отправитель хранит тот же immutable запрос локально: PENDING/status/cancel/retry
  доступны до первого обхода worker без повторного скачивания metadata запроса.
- Сохраняются exact commit/workflow/profile/revision, durable cancel/successor,
  restart recovery без повторного исполнения и однофайловый пакет результата.
- Git exchange/config/repository ID не меняются, старые данные не удаляются.

2026-10-25 оператор отдельно разрешил новый несовместимый формат: «Да, новый
формат; обновить CLI, worker и runtime». Новый клиент проверяет capability
ci_submission_version:1; старый worker не выбирается для новых embedded requests.
Оператор дополнительно указал: совместимость не нужна, старые альфа-репозитории
при необходимости обновляются вручную; worker также обновить. Другие будущие
несовместимые переходы этим решением не разрешены.

## План и критерии приёмки

- [x] Inbox v2 CI envelope с полным request; v1 publication notification сохранён.
- [x] Новый native submit публикует один файл, повтор сохраняет job ID и bytes.
- [x] Offline prepare сразу разрешает upload inbox; accept требует его реальную
  proof и не требует промежуточного request snapshot/check.
- [x] Worker не исполняет повреждённые/чужие запросы и архивирует submission перед CI.
- [x] Статус до регистрации worker, отмена до старта и retry/supervise сохраняют
  исходный request binding; отсутствие snapshot listing не маскируется отсутствием job.
- [x] Проверить lost upload/readback ack, restart/ledger/архивирование, коллизии
  job IDs и неправильные folder/worker/repository/commit bindings.
- [x] Обновить agent.md, inbox/CI protocols и повседневные инструкции.
- [x] Провести проверки до обновления; сохранить BASE+patch/manifest и сырые логи.
- [x] Обновить CLI, worker и опубликованный runtime по согласованному запросу.

## Границы

Сокращается количество файлов и последовательных этапов отправки. Фактическое
время пользовательского коннектора не измерялось этой задачей; 2:35 включает
действия агента, а не только сеть. Передача исходников, Git-протокол, policies
повторяемости, CI stages и пользовательские remote не мигрируют автоматически.


## Реализация и проверки

Новый shared sender передаёт один inbox v2 envelope с полным request v2.
Старые CI inbox v1 и многофайловые agent plans отклоняются; отсутствие нового
capability не вызывает fallback на прежнюю отправку. Отдельная зарегистрированная
конфигурация shell stages остаётся прежним внутренним путём и не является fallback
для общего inbox. Git publication notifications сохраняют свой inbox v1.

Worker фиксирует исходные bytes/route в ledger и проверяет архив submission.json
до claim/checkout/исполнения. Повтор после ошибки доставки сохраняет job/run;
конфликтующий remote archive не перезаписывается. Сохранённый локальный запрос
позволяет наблюдать/отменять job до регистрации. Общий GC учитывает ещё не
зарегистрированные inbox-задания; work GC защищает refs из submission.json.

Целевой прогон: `python3 -m unittest tests.test_ci.LocalAutonomousTests
 tests.test_ci_submission -v` — 15 PASS. Постоянный subprocess worker тестирует
новый shared формат на локальном rclone: FAIL → исправление → PASS → pull --passed.
Сценарии single submission включают один upload, повтор после потери upload ack,
отмену до регистрации, сбой архива, конфликт archive/job ID, чужую identity,
ограничение metadata, отсутствие capability/fallback и offline accept/result
без downloads исходного запроса. [Лог](evidence/TODO_007/targeted-tests.txt).

Точная проверяемая версия: BASE + [source.patch.gz](evidence/TODO_007/source.patch.gz).
Hashes patch/manifest: [checkpoint.json](evidence/TODO_007/checkpoint.json).
[Source manifest](evidence/TODO_007/source-manifest.json) фиксирует Python sources,
tests, test.sh и deploy.py; документация и evidence дополняются после фиксации кода.

Предварительный полный прогон обнаружил устаревший тест, который сохранял
повреждённый второй envelope и ожидал исполнения того же job ID: теперь fixture
явно удаляет конфликт перед повторной доставкой. Следующий предварительный прогон
остановлен после выявления persistent worker fixture со старым implicit fallback;
fixture переведён на config v2 и один inbox-файл. Ещё один существующий тест
ожидал старый fallback при доказанном отсутствии общего capabilities; теперь он
проверяет явный отказ с update worker. Его отдельная проверка — PASS:
[no-fallback-test.txt](evidence/TODO_007/no-fallback-test.txt).
Это изменения тестовых сценариев
под согласованный новый контракт, блокировка неоднозначных запросов сохранена.

Итоговый `bash test.sh`: **OK, 460 tests, 455 PASS / 5 opt-in SKIP**, 282.943 s.
[Сырой лог](evidence/TODO_007/full-tests.txt). Пропущены два Docker и три
user-service opt-in теста; пользовательские CI/hardware runs не выполнялись.
Прогон выполнен вне sandbox-ограничения на Unix sockets для локального rclone RC.
Исходники и tests во время итогового прогона не менялись; hashes сверены после него.

Обязательные software/deployment gates этой задачи выполнены. Реальное время
пользовательского Drive-коннектора и другие worker hosts не проверяются
автоматически этими локальными fixtures.

## Публикация и обновление

Исходный commit `3b5e7af913325b55ac96762a89765b14174b1e4d` отправлен в GitHub
origin/dev; `./deploy.sh` выполнил full GDI publication
`2d9ad7730c0c2eb45d88bec6ddc6a15c75ecdbb7638096ff2a2089d29bcf0191`.
Сборка/smoke zipapp, загрузка gdi.pyz/runtime.json/gdi.pyz.sha256/agent.md в
`gdrive:gdi/gdi/runtime/` и обратное побайтное сравнение — PASS.
[Лог deploy](evidence/TODO_007/deploy.txt),
[машинная запись](evidence/TODO_007/deployment.json).

Runtime HEAD: исходный commit выше; 150708 bytes, SHA256:
`ad7e49768fcbc19f84ecb83c1cd65e7613c822b35d7e30da908c7d3dbfe35906`.
Дополнительный isolated runtime smoke вне source tree выполнил новый prepare,
один upload inbox, accept на исходном snapshot, PENDING до worker и PASS с
verified:true после него. Скачивание submission.json не требовалось, исполнение
было ровно одно. [Сырой итог](evidence/TODO_007/runtime-smoke.txt),
[воспроизводящий скрипт](evidence/TODO_007/runtime-smoke.py).

Локальный CLI использует editable checkout. Перед restart ledger user-dream
не содержал pending jobs. `gdi worker restart` и последующая проверка:
active/running, ExecMainStatus=0, MainPID=875687,
ActiveEnterTimestamp=2026-10-25 21:32:06 MSK.
Свежие capabilities с Drive: worker_id=user-dream, ci_submission_version=1,
result_delivery_version=1. [Restart](evidence/TODO_007/worker-restart.txt).

Документационная запись о завершении добавляется отдельным commit после deploy;
она не меняет проверенный код runtime. Уже работающим контейнерам и другим host
нужно получить новый опубликованный runtime/код; дистанционное обновление
неуправляемых hosts не выполнялось. Старые remote/очереди не мигрировались и
не удалялись; их ручное обновление остаётся решением оператора.
