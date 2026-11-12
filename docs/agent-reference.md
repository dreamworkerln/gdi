# GDI 0.8: специальные CI/work операции

Основной Git-обмен — только agent pull/push из agent.md. Этот справочник не
предписывает запускать CI, создавать ветки или work workflow без разрешения.
Старые capture/snapshot-build/prepare/check/accept для публикации Git не нужны.

## Небольшие offline inputs v2

CI/work команды принимают --snapshot с локальным JSON snapshot_version=2.
Это не capture оригинальных ответов коннектора и не полный снимок Drive.
Укажите ранее скачанный head JSON и только необходимые файлы/списки:

```json
{
  "snapshot_version": 2,
  "repository_id": "TRUSTED_ID",
  "repository_path": "my-project",
  "ref": "refs/heads/main",
  "head": "head.json",
  "files": {},
  "listings": {}
}
```

Все local paths задаются относительно этого JSON. files — словарь
`"remote/relative/path": "downloaded/local/path"`. listings — словарь
`"remote/folder": [{"Path":"child-name","IsDir":false,"Size":123}]`.
Для отсутствующего каталога достаточно полного списка его родителя, который
показывает отсутствие имени. Не представляйте нескачанный существующий файл
как отсутствующий. Для существующих каталогов CI нужен только их адресный список.
GDI проверяет скачанное содержимое локально. Поля JSON можно сформировать локальным
скриптом без Google Docs, шифрования и сохранения полного дерева Drive.

Для CI на только что опубликованном HEAD используйте уже имеющийся head JSON из
локальной базы/квитанции push. Повторное скачивание head или ZIP для prepare не нужно.
Для результата старой публикации добавьте её скачанный ZIP в files под точным
`packets/<publication_id>.zip`, если он требуется команде и отсутствует локально.

## Запросить CI

Получите/переиспользуйте capabilities выбранного worker. Публикация Git и запрос
CI — отдельные действия. Подготовьте один CI inbox-файл:

```bash
python3 /mnt/data/gdi.pyz agent ci prepare \
  --repo /mnt/data/checkouts/project --repository-id TRUSTED_ID \
  --snapshot /mnt/data/exchange/ci-inputs.json \
  --publication PUBLICATION_ID --worker WORKER_ID --profile full \
  --capabilities /mnt/data/exchange/capabilities.json \
  --output /mnt/data/exchange/ci-plan
```

Загрузите только files.inbox в общую inbox/. Успешный upload означает отправку.
Не скачивайте запрос обратно, не готовьте inbox proof и не вызывайте accept
ради дополнительной сетевой проверки. Saved prepared plan пригоден для result.
Если требуется локально отметить отправку, `agent ci accept --uploaded` обновляет
план без download proof. Старый proof интерфейс относится к историческим v3 inputs.

## Получить результат

После локального ожидания запросите только каталог нужного `ci/jobs/JOB_ID`.
Если появился result-<sha256>.zip, скачайте этот один пакет и добавьте его в
files, а список job-каталога — в listings. Затем:

```bash
python3 /mnt/data/gdi.pyz agent ci result \
  --repo /mnt/data/checkouts/project --repository-id TRUSTED_ID \
  --snapshot /mnt/data/exchange/ci-inputs.json --plan /mnt/data/exchange/ci-plan
```

Пакет содержит итог, лог и artifacts. Не скачивайте их по одному и не собирайте
log chunks. GDI проверяет пакет локально; verified:true и state определяют результат.
Не опрашивайте корень Drive или все репозитории. Частые циклы коннектора ради
ожидания запрещены; отсутствие результата сообщайте как PENDING. Полный лог
изучается локально после одного получения. FAIL не является разрешением retry
без правил задачи; повтор — новый job ID, исходный результат сохраняется.

## Явно согласованный work workflow

До создания `gdi-work/<ID>` назвать ветку и squash при import и получить согласие.
Work-ID — положительное число оператора; номер TODO и worker ID для этого не подходят.
В inputs v2 укажите head выбранной target-ветки, адресные listings heads/ и
work/retired/; при наличии интересующего JSON скачайте только его.

```bash
python3 /mnt/data/gdi.pyz agent work start ID \
  --repo /mnt/data/checkouts/project --repository-id TRUSTED_ID \
  --snapshot /mnt/data/exchange/work-inputs.json
python3 /mnt/data/gdi.pyz agent work checkpoint \
  --repo /mnt/data/checkouts/project --repository-id TRUSTED_ID -m 'Описание'
```

При завершении добавить --final. Опубликовать work-ветку тем же agent push —
один ZIP и квитанция. Новую work-ветку worker примет только при свободном ref;
занятый или retired ID не переиспользуется. Оператор выполняет gdi import отдельно.
Обычная работа в выбранной ветке не требует ни этих команд, ни Work-ID.
