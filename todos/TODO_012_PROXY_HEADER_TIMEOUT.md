# TODO_012 — Автопереключение на env proxy при response-header timeout

Дата 2026-11-12. База dev@13adde627c3a280ac37109e083ce2a82d78b4ce3.
Источник: оператор показал ошибку push после 100% ZIP и уточнил требование
автодетекта/переключения на proxy из env.
Status IMPLEMENTED; Implementation DONE; Validation PASS локального test.sh:
532 tests, 527 PASS / 5 opt-in SKIP. Real Drive acceptance открыт.

## Диагностика и контракт

Локальный packet 553567e0b523593caa0b4ed0c01aa0de78e57bbaa44bc487129a7b2ea426586d
имеет 1090691 bytes, SHA256 совпадает с сохранённым plan; phase UPLOADING.
Эта отправка не дошла до update head. Наличие packet на Drive не проверялось.
GDI задаёт timeout 60 s; в rclone v1.75.1 fs/fshttp/http.go этот timeout используется
как ResponseHeaderTimeout. Источник задержки Google/сеть/proxy не установлен.

Автодетект env/systemd/shell proxy уже был реализован. Дефект: network_error
пропускал буквальное `net/http: timeout awaiting response headers`, хотя другой
вариант timeout из Go HTTP был учтён. Поэтому NetworkRoutes не переходил к
доступной альтернативе после данного сбоя.

Исправить классификацию, сохранив порядок выбора/кеш успешного маршрута,
HTTPS_PROXY/https_proxy/ALL_PROXY/all_proxy, NO_PROXY, один проход по кандидатам
и отсутствие proxy URL/паролей в журнале и state. Для RC переключать весь transfer
с restart daemon под новым env, а не повторять local job/status в другом daemon.
Уже существующие immutable/checksum параметры upload сохраняются.
Timeout, протокол v4, миграции и количество обычных успешных Drive-операций не меняются.

## Реализация и проверки

- [x] Добавлен точный маркер response-header timeout в network_error.
- [x] Добавлены четыре regression tests: env variants/cached direct и запоминание
  proxy; CLI retry с новым env; RC async upload и daemon restart; каждый маршрут
  только один раз при повторных таймаутах. Проверки redaction и NO_PROXY включены.
- [x] Документация worker обновлена; git diff --check и ast.parse PASS.
- [x] Полный test.sh по запросу оператора, require_escalated, без наблюдения:
  532 tests за 372.544 s, 527 PASS / 5 opt-in SKIP, exit 0.
- [ ] Реальный Google Drive acceptance по отдельному разрешению.

После разрешённого прогона оператор отдельно запросил Git commit всех изменений.
Push, deploy, перезапуск worker и запросы Drive здесь не выполнялись.
Исходные файлы service .gdi и кеш предпочтений сети не редактировались вручную.
Проверено исходное сопоставление маркеров: ошибка не распознавалась. После
исправления локальный полный прогон PASS с пятью opt-in SKIP. Исходники rclone сверены
с официальным tag v1.75.1, fs/fshttp/http.go (ResponseHeaderTimeout = ci.Timeout).

Лог: `/tmp/gdi-proxy-tests-20261112-YvRkaT/output.log`, exit-code: 0.
Исполняемые исходники и tests после старта прогона не менялись. Рабочая правка
относительно BASE 13adde6 сохранена в source.patch/source.patch.sha256 рядом
с логом перед commit; обновлённые после проверки TODO/реестр входят в этот снимок.
