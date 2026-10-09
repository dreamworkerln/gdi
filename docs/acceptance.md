# Эксплуатационная проверка v2 в отдельных fixtures

Эти проверки не используют `lora-sack/chatter`. У каждого запуска собственные Git
репозитории, worker ID/state/cache, общий root и проект. Обычный unittest не обращается
к Google Drive и не запускает Docker; интеграции включаются явными environment flags.

## Локальный CLI и настоящий act

Укажите абсолютный путь к act **v0.2.89**; rclone должен находиться в PATH:

```bash
GDI_TEST_ACT=/absolute/path/act PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v2_cli.LocalV2CliTests -v
```

Проверяется полный CLI цикл FAIL → console → исправленный commit → PASS → pull
точного SHA, matrix/needs/optional failure, namespace GitHub и тестовый secret.
Отдельно проверяются timeout, SIGINT с завершением текущего job и SIGKILL/restart
с INTERRUPTED без повторного исполнения. Runner использует `-self-hosted`; Docker
совместимость этим запуском не подтверждается. Act открывает локальные servers
artifacts/cache; окружение должно разрешать bind TCP.

## Реальный Docker runner

Docker должен быть установлен и доступен пользователю. Загрузите образ с Node/Python,
совместимый с act, затем включите только Docker fixtures:

```bash
docker pull catthehacker/ubuntu:act-latest
GDI_TEST_ACT=/absolute/path/act GDI_TEST_DOCKER=catthehacker/ubuntu:act-latest PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v2_cli.DockerV2CliTests -v
```

Проверяются matrix/needs, secrets, actions/checkout@v4 и upload-artifact@v3/@v4,
сбор artifact store, exact pull, SIGKILL и cleanup containers act. Actions скачиваются
из GitHub; нужен доступ к сети. Совместимость другого workflow/action оценивайте
отдельно. После успешного запуска можно закрепить полученный digest образа в config.
Те же Docker fixtures включены отдельным job в `.github/workflows/ci.yml`.
Для Docker fixtures лимит исполнения составляет 600 секунд, чтобы первая загрузка
external actions не обрывала проверку. CLI wait допускает 720 секунд; обычные
локальные fixtures сохраняют короткие лимиты. Для настоящего Drive CLI subprocess
допускает 900 секунд из-за отдельных rclone metadata/upload операций.

## Настоящий Google Drive

Только в разрешённой отдельной папке Drive. Пример для локального remote `rclone:`:

```bash
GDI_TEST_ACT=/absolute/path/act GDI_TEST_DRIVE_ROOT=rclone:gdi-acceptance PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v2_cli.LocalV2CliTests.test_fail_fix_pass_and_pull_via_cli -v
```

Этот запуск создаёт на Drive уникальную подпапку `gdi-v2-cli-*` с проектом, inbox,
capabilities и CI artifacts. Она сохраняется для ручной проверки; локальные fixtures
удаляются. Исходные repository remotes и credentials не изменяются. Rclone может
обновить OAuth token в своей конфигурации. Для проверенного Docker окружения можно
совместно задать `GDI_TEST_DOCKER` и выбрать `DockerV2CliTests`.

## Systemd и длительная эксплуатация

После проверки foreground runner настройте отдельный постоянный host config,
абсолютные act/Python/rclone paths и dedicated Drive root. Команды установки и
настройки user service: [worker.md](worker.md), [INSTALL.md](../INSTALL.md).
На машине владельца требуется подтвердить:

1. `worker check --runtime`, доступ к Drive и capabilities в нужном root.
2. Install/start/status и CLI цикл через службу; stdout/stderr находятся в journal.
3. `worker stop` во время job: завершение до TimeoutStopSec или INTERRUPTED после restart.
4. Logout/reboot при выбранной политике linger; worker возвращается и не повторяет CI.
5. Достаточное место под state/spool/cache; CI logs и artifact retention пока ручные.

User service, linger и reboot меняют состояние реального host. Автоматические
fixtures не объявляют эти проверки выполненными. Итоговый статус сохраняется в
[TODO.md](../TODO.md) с версиями, командами и фактическими результатами.

Для уже установленной **отдельной acceptance-службы** есть opt-in проверки.
В её config должны быть Docker platform и `secret_names: ["GDI_FIXTURE_SECRET"]`,
а в приватном EnvironmentFile — `GDI_FIXTURE_SECRET=fixture-value` и нужные host
proxy/PATH/rclone настройки. Служба должна быть активна перед запуском:

Для сетевых acceptance fixtures задайте `TimeoutStopSec=900` через unit override:
штатная остановка ждёт не только CI, но и доставки его artifacts/result в Drive.
Процесс, запускающий тесты, также должен иметь доступ к Docker socket; после
изменения групп в текущей сессии можно запускать тесты через `sg docker`.

```bash
GDI_TEST_ACT=/absolute/path/act GDI_TEST_SERVICE_CONFIG=/absolute/path/acceptance-worker.json PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v2_cli.ServiceV2CliTests -v
```

Fixtures создают уникальные проекты в `projects/gdi-v2-cli-*` общего root службы.
Проверяются FAIL → fix → PASS/exact pull с Docker/artifacts, штатный stop во время
job и SIGKILL всей systemd control group с recovery в INTERRUPTED без rerun.
После terminal result проверяется отсутствие принадлежащих checkout containers;
тест не выполняет дополнительную cleanup, которая могла бы скрыть ошибку worker.
Remote проекты и постоянный spool остаются для проверки; служба остаётся включённой.
Эти тесты останавливают/перезапускают `gdi-worker.service`, поэтому используйте только
выделенную acceptance-службу. Logout/reboot по-прежнему проверяются отдельно владельцем.
