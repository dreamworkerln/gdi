# TODO и состояние gdi 0.3

Автономный CI реализован. Проверки выполняются на Python 3.10.12; настоящий
Google Drive и служба на машине пользователя остаются отдельным эксплуатационным
шагом. Команды и настройка: [QUICKSTART.md](QUICKSTART.md), [Install.md](Install.md).
Архитектура: [ARCHITECTURE.md](ARCHITECTURE.md).

## Реализовано

- [x] Git protocol v2: полные checkpoints, incremental bundles, prerequisite chains.
- [x] Проверки SHA256/размера/ref/HEAD/ancestry, private quarantine и persistent verified cache.
- [x] Безопасный fetch и чистый fast-forward pull, обнаружение конфликтующих публикаций.
- [x] Ручной GC всех веток: dry run, восстановление с нуля, повторная сверка snapshots.
- [x] CI protocol v1: strict request/result identities, ready last, result last.
- [x] `push --ci`, `ci submit/status/wait/logs/retry`, JSON для автоматизации.
- [x] Durable agent outbox, повтор submit с тем же job ID, retry завершённого job новым ID.
- [x] Local worker config: registered repository IDs, profiles, computed revisions, capabilities.
- [x] Команды профиля задаёт host; request не может подменить argv или ослабить проверки.
- [x] Постоянный worker, активная queue отдельно от архива jobs, последовательное исполнение.
- [x] Изолированный detached checkout точного SHA, без изменения пользовательского worktree.
- [x] Persistent worker cache; получение старого job SHA из нового checkpoint после bundle GC.
- [x] Blocking/non-blocking стадии, warnings, stage/global timeouts и process-group termination.
- [x] HEAD/tracked sources проверяются после CI, required artifacts и отказ symlink escape.
- [x] Binary stdout+stderr в файл, независимый live publisher, heartbeat, immutable log chunks/events.
- [x] Проверка полного результата, artifacts и совпадения chunks с полным log до принятия PASS.
- [x] `pull --passed --job --profile` применяет ровно SHA выбранного PASS, ff-only.
- [x] SQLite WAL/FULL ledger, persistent XDG spool, atomic fsync writes.
- [x] Upload/restart recovery без повторного CI, lost acknowledgement delivery retry.
- [x] Неопределённое исполнение даёт INTERRUPTED, чужой claim не перехватывается.
- [x] Linux process identity проверяется перед остановкой восстановленного процесса.
- [x] `worker check/run/install/start/status/stop`, foreground и systemd user service.
- [x] Worker diagnostics в journal, per-job console отдельно; служба не останавливается после PASS/FAIL.
- [x] Apply-GC отказывает при queue entries и incomplete/nonterminal CI jobs.
- [x] Документация установки, host profiles, service, recovery, инструкция агенту и краткий справочник.

## Проверено автоматически

- [x] Настоящие временные Git repos; существующие exchange/GC regression tests.
- [x] Настоящий rclone local backend и worker отдельным постоянным процессом.
- [x] Агент: FAIL → чтение консоли → новый исправленный commit → PASS.
- [x] Прогресс и ранняя консоль видны до завершения CI; worker остаётся работать.
- [x] Пользовательский worktree нетронут, pull PASS не берёт новый непроверенный tip.
- [x] Upload result failure и restart без второго исполнения команд.
- [x] Lost acknowledgement после result upload; повтор доставки того же результата.
- [x] RUNNING без durable result → INTERRUPTED без rerun.
- [x] Неверный ready/foreign claim не исполняется; смена revision даёт REJECTED.
- [x] Binary/большая консоль, log checksum, missing chunks, обязательные blocking stages.
- [x] TIMEOUT и уничтожение descendants, удерживающих stdout.
- [x] Required artifact missing, разрешённый binary artifact, symlink outside.
- [x] Dirty/divergent pull gate сохраняет пользовательские изменения.
- [x] Старый commit после bundle GC и удаления worker cache.
- [x] Timeout клиента не отменяет job; повтор submit/retry имеет определённую семантику.

## Перед эксплуатацией на реальном host

- [ ] Владелец host выбирает и проверяет полный CI своего проекта, устанавливает его зависимости.
- [ ] Прогнать тот же цикл на настоящем Google Drive/rclone OAuth в разрешённом окружении.
- [ ] Установить user service, проверить PATH/rclone config, logout/reboot/linger на конкретном host.
- [ ] Проверить принудительную остановку/reboot именно с полным проектным CI и его процессами.
- [ ] Для длительной нагрузки определить доступное место под локальный spool и remote CI logs.

Наличие локального интеграционного теста не объявляет эти эксплуатационные проверки
выполненными. gdi не меняет credentials, linger и службы автоматически при установке пакета.

## Дальнейшее развитие

- [ ] Шире fault matrix: crash на каждой границе ledger/exec/fsync/upload и сбой каждого artifact.
- [ ] Сохранённый cursor логов между CLI вызовами; сейчас новый follow повторяет историю с начала.
- [ ] Явное выделение stale heartbeat и подробные environment/tool version reports.
- [ ] Политика local retention, ограничения log/spool size и безопасная очистка checkout после доставки.
- [ ] Отдельный CI artifacts/logs GC: dry run, retention и защита ещё нужных результатов.
- [ ] Bundle GC с pins pending jobs вместо запрета обслуживания при incomplete jobs.
- [ ] Управляемая отмена job с подтверждённым terminal state и завершением процессов.
- [ ] Автоматический выбор последнего PASS без явного job, с правилами повторных проверок.
- [ ] Оптимизация metadata listings, Drive changes cursor/API после проверки текущих ограничений.
- [ ] Опциональные notifications/PubSub; polling остаётся рабочей базой.
- [ ] Multi-worker execution только с отдельной координацией; immutable marker не CAS.
- [ ] Опциональная OS/container изоляция исполнения.

Не заявляются: exactly-once, распределённый lock, произвольный Git force push,
LFS/submodules, автоматический GitHub merge/push или настройка Google credentials.
