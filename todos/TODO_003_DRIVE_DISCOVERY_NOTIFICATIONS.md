# TODO_003 — Drive Changes API и опциональные notifications

Status: OPEN / MIGRATED.
Implementation: UNKNOWN; пункты перенесены без ревизии реализации.
Validation: NOT RUN.
Дата: 2026-10-21. Checkpoint миграции: `20c6359de147a0dc66f542d3a467a24685020a39`.
Источник: [исходный TODO](history/LEGACY_TODO_20261021.md), «Дальнейшая архитектура».

## Сохранённый backlog


- [ ] Изучить Drive Changes API/cursor для сокращения повторных listings.
- [ ] Добавить опциональные notifications/PubSub, сохранив polling.

## Цель и границы

Сократить повторные listings, сохранив надёжное обнаружение immutable публикаций.
Это исследовательский backlog: конкретные API, credentials/scopes, миграция cursors
и критерий выигрыша ещё не выбраны. Не менять формат обмена и требования свежести
на основании одного этого TODO; notifications не являются единственным источником
истины или разрешением отказаться от polling.

## План приёмки

- [ ] Изучить применимость Changes API и определить базовые метрики listings/latency.
- [ ] Согласовать контракт восстановления cursor и fallback к полному listing.
- [ ] Определить отдельно opt-in notifications/PubSub и сохранение polling.
- [ ] Реализовать выбранное решение после согласования scope/совместимости.
- [ ] По явному запросу проверить пропущенные/повторные события, expired cursor,
  reconnect, quota/errors и восстановление; сохранить exact revision и evidence.
- [ ] Обновить протокол/пользовательские инструкции по фактическому поведению.

Реальные обращения к Drive/PubSub и изменение настроек требуют отдельного разрешения.
