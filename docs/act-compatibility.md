# act и новые версии artifact actions

В act `0.2.89` сервер артефактов отклоняет поле `mime_type` из
`actions/upload-artifact@v7`. После исправления этого поля отдельно требуется
поддержать URL-подпись без завершающего `=`. Это ошибка host runner, а не
публикации gdi или исходников проверяемого проекта.

До появления подходящего релиза можно собрать act `0.2.89` с
[upstream PR #6115](https://github.com/nektos/act/pull/6115), commit
`34fc9c523f2ea43ff90f720b4d2b001a00c91dca`. Проверенная копия патча находится в
[tools/act-v0.2.89-pr6115.patch](../tools/act-v0.2.89-pr6115.patch).
Это локальная сборка с upstream-патчем, а не официальный исправленный релиз.
Поддержка прямой загрузки без ZIP (`archive: false`) этим патчем не подтверждена.

Для воспроизводимой сборки нужны Go согласно `go.mod`, curl, tar и git.
Команды выполняются в отдельном каталоге; `GDI_SOURCE` — путь к исходникам gdi:

```bash
set -eu
GDI_SOURCE=/path/to/gdi
curl --fail --location --output act-v0.2.89.tar.gz \
  https://codeload.github.com/nektos/act/tar.gz/refs/tags/v0.2.89
sha256sum --check <<'CHECKSUM'
649cd5b91cad870871d2283fb3ad95c8fa1d5ced7a1db8d7b346d1a7dcd3ec71  act-v0.2.89.tar.gz
CHECKSUM
tar -xzf act-v0.2.89.tar.gz
cd act-0.2.89
git apply --check "$GDI_SOURCE/tools/act-v0.2.89-pr6115.patch"
git apply "$GDI_SOURCE/tools/act-v0.2.89-pr6115.patch"
go test -short ./pkg/artifacts/
CGO_ENABLED=0 go build -trimpath -ldflags '-X main.version=0.2.89' \
  -o ../act-v0.2.89-pr6115 .
../act-v0.2.89-pr6115 --version
sha256sum ../act-v0.2.89-pr6115
```

В `worker.json` укажите абсолютный путь новой сборки в `act_executable` и
сохраните `act_version: "0.2.89"`. Не заменяйте текущий бинарник до проверки.
`gdi worker check --config /path/to/worker.json --runtime --json` учитывает
SHA256 самого бинарника в execution revision, поэтому локальная сборка
получит новую revision даже с прежним номером версии. Перезапустите worker
после окончания текущего CI и его доставки. Новые запросы создавайте через
существующие команды gdi по обновлённым capabilities.

Перед повтором рабочего CI проверьте в отдельном тестовом репозитории
upload/download ZIP-артефакта (`upload-artifact@v7`, `download-artifact@v8`),
затем убедитесь через gdi, что итоговый артефакт доставлен и проверен.
Не исправляйте эту ошибку подменой cached actions или ручным изменением
заданий на Drive. При переходе на официальный исправленный релиз снова
проверьте runtime и обновите capabilities перезапуском worker.
