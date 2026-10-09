# Инструкция агенту: автономный CI через gdi

Актуально для gdi 0.3.0: Git protocol v3, CI protocol v1/v2. Агент публикует точный
commit, постоянный worker пользователя запускает выбранные `.github/workflows` через act,
агент получает проверенный результат и весь консольный вывод. Ручная передача логов
между пользователем и агентом не требуется.

Настройка host: [INSTALL.md](INSTALL.md), [docs/worker.md](docs/worker.md).
Человеческий справочник: [QUICKSTART.md](QUICKSTART.md).

## Где выполняются агент и CI

Агент может работать в облачном контейнере провайдера, а постоянный worker —
на компьютере пользователя. Google Drive связывает эти две среды: агент
передаёт Git-публикации и CI requests, worker локально выполняет act/Docker
и публикует консоль, artifacts и result. Рабочий clone пользователя не меняется.

Далее описаны команды установленного CLI. Если в контейнере агента GDI нет
и установить его нельзя, используйте раздел [«Облачный агент без установленного GDI»](#облачный-агент-без-установленного-gdi):
он описывает получение commit, публикацию собственных изменений, отправку версии
на CI и передачу результата пользователю через инструменты Drive, Python и Git без CLI.

## Перед первой отправкой

Прочитайте действующие инструкции репозитория. Убедитесь, что задача разрешает
создание commits и отправку на CI. Уже полученное разрешение сохраняется на цикл
исправлений; не просите пользователя повторять его на каждой итерации.

Должны быть известны рабочая ветка, gdi remote, Repository ID, ID worker и название
полного профиля. Например: `dev`, `drive`, известный ID, `user-host`, `full`.
Обычный Git remote и gdi remote — разные настройки; `gdi remote list` показывает
именно подключения через Drive.

```bash
gdi -v
gdi remote list
git status --short
git branch --show-current
```

Первое подключение к уже созданной папке:

```bash
gdi remote add drive gdrive:gdi/my-project --repository-id REPOSITORY_ID
```

Не используйте `--init` на существующей папке. Не устанавливайте службу и не меняйте
credentials пользователя в рамках задачи с исходниками. Host должен уже быть
настроен его владельцем. Не запускайте второй worker для того же remote.

## Исходники → commit → job

Изучите задачу, измените исходники, выполните доступные локальные проверки.
Коммитьте только относящиеся к задаче файлы. Проверьте отсутствие credentials и
лишних build outputs в commit.

```bash
git add path/to/changed-file
git commit -m "Fix requested behavior"
git rev-parse HEAD
gdi push drive --ci --worker user-host --profile full --json
```

gdi сам commit не создаёт. Незакоммиченные изменения не отправляются. Несколько commits
можно отправить одним push, CI проверит выбранный HEAD. Не изменяйте ту же ветку
параллельно с push. Ответ JSON содержит `job_id`, `head`, `ref`, `publication_id`,
`worker_id`, `profile_id`, `profile_revision` и `repository_id`; сохраните их.

Request использует возвращённую Git-публикацию. Изменение локального HEAD после
отправки не меняет задание. Профиль и revision берутся из capabilities worker.
Если worker не объявил общие execution capabilities, submit завершится ошибкой, хотя Git push
может уже успешно завершиться. После настройки повторите ту же команду.

Для ранее опубликованной версии:

```bash
gdi ci submit drive --publication PUBLICATION_ID --worker user-host --profile full --json
```

Outbox сохраняется в `.gdi/ci/<repository-id>/outbox/` основного worktree до upload.
Повтор после сетевой ошибки допубликовывает те же bytes с тем же job ID. Повтор
успешного submit возвращает прежний job, даже если он уже завершён. Не удаляйте outbox
ради повторного исполнения; используйте `ci retry`. После потери outbox сначала
проверьте ранее сохранённый ID, глобальная дедупликация не обещается.

## Следить за исполнением и получить консоль

```bash
gdi ci status drive JOB_ID --json
gdi ci wait drive JOB_ID --follow --timeout 3600 --json
```

`wait --json` выдаёт один итоговый JSON в stdout; прогресс, stage, heartbeat и
консоль идут в stderr. Для автоматизации сохраняйте потоки отдельно. `verified: true`
означает, что terminal result, идентичность request и контрольные суммы всех artifacts
проверены, полный log совпал с непрерывной последовательностью chunks.

Полный лог можно сохранить после завершения, включая FAIL:

```bash
gdi ci logs drive JOB_ID --output /tmp/gdi-JOB_ID.log
```

Без `--output` команда показывает уже опубликованные фрагменты; с `--follow` ждёт
окончания и показывает новые. Бинарные bytes сохраняются без преобразований.
В локальной `.gdi` основного worktree также остаются проверенные result и artifacts:
`ci/<repository-id>/results/<job-id>/`.

| Exit `ci wait` | Значение |
| --- | --- |
| 0 | Проверенный PASS |
| 1 | Проверенный FAIL/ERROR/TIMEOUT/INTERRUPTED/REJECTED либо ошибка gdi; различайте JSON `state` и `kind: GDI_ERROR` |
| 2 | Ошибка аргументов |
| 124 | Истёк timeout ожидания клиента; job продолжает работать |
| 130 | Прервано ожидание, job не отменён |

Status и logs возвращают 0 при успешном чтении независимо от исхода CI.
При сетевой ошибке ожидание можно продолжить тем же ID. Advisory `status.json`
не доказывает PASS и может отставать; терминальным доказательством служит проверенный result.

## Итерации до PASS

| Итог | Действие агента |
| --- | --- |
| FAIL | Читать `failed_stage`, `exit_code` и полный лог; исправить исходники, создать новый commit, новый `push --ci` |
| ERROR | Разобрать `detail`, лог и ошибки подготовки/инструментов/artifacts; менять host config только в разрешённом объёме |
| TIMEOUT | Проверить зависание; host ограничивает время исполнения отдельно от времени ожидания агента |
| INTERRUPTED | Исполнение после сбоя неопределённо; изучить лог и явно запросить новую попытку |
| REJECTED | Профиль неизвестен/изменился; после настройки отправить новую заявку с текущей revision |
| PASS | Проверить точные head/profile/revision, сообщить итог и способ получить проверенный commit |

```bash
# Только для завершённой попытки на прежнем commit:
gdi ci retry drive JOB_ID --json
```

Retry создаёт новый ID с `retry_of`, сохраняя выбранную публикацию и используя текущую
revision профиля. Повтор самой команды retry переиспользует созданную попытку через
outbox. Для ещё активного job retry запрещён. Upload готового результата worker
повторяет самостоятельно; отправлять новый job из-за временной ошибки upload не нужно.
Worker после PASS и FAIL продолжает принимать задания. STOP-файлов нет.

## Передать результат пользователю

Сообщите commit SHA, job ID, профиль/revision, исход CI и существенные детали проверки.
У пользователя исходный worktree не менялся. Чтобы получить именно проверенный commit:

```bash
gdi pull drive --passed --job JOB_ID --profile full
```

Команда проверяет выбранный PASS и применяет его SHA через fast-forward на текущей
совпадающей ветке. Более новый непроверенный tip не подставляется. Dirty/divergent
дерево требует обычного разрешения средствами Git; gdi его не сбрасывает.

Для чтения истории без изменения файлов есть `gdi fetch drive`. Обычный `gdi pull drive`
выбирает последний опубликованный tip без CI gate; не используйте его как эквивалент
`pull --passed`.

## Обслуживание

GC bundles выполняется отдельно между задачами. Все результаты должны быть доставлены,
очередь пуста, worker и остальные клиенты приостановлены. `gdi gc drive` — только план;
`--apply --quiescent` проверяет сохранённую историю и отказывает при незавершённых CI
jobs. Старый gdi 0.2.1 не знает CI-защиту GC: обновите все клиенты до 0.3 перед применением.
Сами CI logs/results и локальный spool автоматически не очищаются.

В config version 2 worker опрашивает общую inbox всех проектов. Список репозиториев
и команды CI в host config не требуются. Для выбора проверки используйте
`--workflow PATH`, `--event EVENT`, `--job ID`, `--input KEY=VALUE`; selector закрепляется
в request и сохраняется при retry. Не добавляйте shell-команды в другие configs.
Подключения хранятся в `.gdi/config.json`, Git config не изменяется. `.gdi/` должна
оставаться локальной и не попадать в commits. Протокол: [docs/inbox.md](docs/inbox.md).

Execution revision связывает настройки с фактическим act/Docker/base image окружением.
После обновления host tools/images требуется restart worker; новые capabilities
могут потребовать resubmit. Проверяемый отчёт — `artifacts/environment.json`.
Namespace GitHub из обычного origin закрепляется в request v2 и сохраняется при retry.
Перед push CLI проверяет синтаксис selector и наличие обычных tracked YAML файлов
в выбранном commit для global worker. YAML/job/event semantics проверяет act.

## Облачный агент без установленного GDI

Этот вариант рассчитан на агент в контейнере провайдера и уже запущенный worker
на компьютере пользователя. Агент получает Git-историю и публикует запросы через
инструменты Google Drive; worker локально выполняет act/Docker и возвращает
консоль, artifacts и result через тот же Drive. Агенту не нужны локальный Docker,
служба worker или credentials пользователя.

Нужны инструменты для поиска/listing папок, скачивания исходных bytes обычных
файлов (включая бинарные `.bundle`/artifacts) и загрузки обычных файлов с сохранением
точных bytes. Поиск по тексту или преобразование JSON в Google Docs не заменяют
эти операции. Сначала проверьте доступные возможности. Если записи на Drive нет,
агент может читать уже созданное задание, но не должен заявлять, что отправил новое.
Также нужны Python 3.10+ и Git в среде агента. Описанная ниже публикация не требует
установленного пакета GDI, rclone, Docker или worker в этой среде: операции Drive
выполняются доступными инструментами агента. Если нельзя получить/загрузить точные
бинарные bytes, этот способ недоступен; явно сообщите ограничение и передайте patch
или архив другим доступным способом, не обещая, что `gdi pull` получит изменения.

Пользователь сообщает общий Drive root, путь проекта внутри root, Repository ID,
ветку, точный HEAD и Publication ID, worker ID, profile и выбранные workflow/event.
Например, root `gdi`, проект `my-project`, ref `refs/heads/dev`, worker `user-host`,
profile `full`. `gdrive:`/`rclone:` — локальные имена remotes на компьютере
пользователя; облачный агент находит саму папку Drive своими инструментами.
Пути ниже относительны общему root; `PROJECT_PATH` — путь проекта внутри root.

### Получить уже опубликованный commit

1. Скачайте `PROJECT_PATH/repository.json`; проверьте protocol `version:3`,
   `object_format:"sha1"` и известный Repository ID.
2. Возьмите имя ветки из ref: для `refs/heads/dev` это `dev`.
   Закодируйте сначала `%` как `%25`, затем `/` как `%2F`; Unicode сохраните.
   Скачайте `PROJECT_PATH/branches/<encoded-branch>/<publication-id>.json`.
   SHA256 исходных bytes manifest должен совпасть с Publication ID; сверяйте
   `repository_id`, `ref` и `head` с выбранной публикацией.
3. Для полного bundle проверьте `bundle_kind:"full"`, `base_publication:null`,
   `base_head:null` и `prerequisites:[]`. Скачайте
   `PROJECT_PATH/bundles/<bundle_sha256>.bundle`, проверьте SHA256 и `bundle_bytes`.
   Публикация может быть incremental: тогда нужны полный checkpoint и цепочка
   её баз по `base_publication`, применённая по порядку. Не импортируйте дельту
   в пустой clone; порядок и проверки описаны в [docs/protocol.md](docs/protocol.md).
4. Проверьте bundle средствами Git в отдельном временном repository, включая
   фактические prerequisites, полный ref и точный HEAD. Полный bundle проверяется
   в пустом bare repository; затем восстановите рабочий clone и выбранную ветку.
   Для дельт проверяйте наличие prerequisites и ancestry к объявленной базе.
5. Убедитесь, что рабочий HEAD равен выбранному SHA; прочитайте инструкции
   репозитория, включая `AGENTS.md`, из этого commit. Не подменяйте выбранный
   commit более новым tip. Проверка отдельного bundle не заменяет проверку всей
   metadata-цепочки: учитывайте `previous`, отсутствующих предков и конкурирующие
   продолжения по [Git protocol v3](docs/protocol.md).

### Опубликовать собственные изменения без GDI и rclone

Далее — полноценная Git-публикация, которую пользователь получает обычным GDI.
Для ручного writer используйте **полный bundle**: он позволяет передать всю
историю выбранной ветки без подготовки incremental prerequisites. Полный bundle
не обнуляет цепочку публикаций: `previous` остаётся ID прежнего tip этой ветки.
Не создавайте новый repository ID и не инициализируйте существующую папку заново.

#### 1. Проверить текущую базу на Drive

До создания публикации скачайте свежие `repository.json` и **все manifests выбранной
ветки** из `PROJECT_PATH/branches/<encoded-branch>/`. Используйте полный listing,
учитывайте его pagination и обнаруживайте повторяющиеся имена файлов/папок;
поиск только «последнего» manifest или сортировка по времени не заменяют listing.
Имена каталогов `%2F` и `%25` здесь буквальные, а не инструкции декодировать путь.

- Repository ID должен совпадать с доверенным ID пользователя, version — 3,
  object format — SHA-1. При подмене ID остановитесь, не принимайте новый автоматически.
- Для каждого manifest проверьте SHA256 исходных bytes по имени файла, точную
  схему protocol v3, repository ID, ref, HEAD, nonce и поля bundle/base/prerequisites.
  Отклоняйте дубли JSON-ключей, неизвестные поля и metadata больше 1 MiB.
- Постройте всю цепочку через `previous`: один корень с `previous:null`, не более
  одного продолжения каждого предыдущего ID, все предки доступны, нет циклов
  и несвязанных записей. Incremental bases должны указывать на более раннюю
  публикацию с точным base HEAD; проверяйте все правила [протокола](docs/protocol.md).
- Сохраните снимок: имена и точные bytes всех manifests, Repository ID и ref.
  `PREVIOUS_PUBLICATION` — ID единственного tip, `BASE_HEAD` — его HEAD.
  Обе переменные пусты только при подтверждённо пустой цепочке этой ветки.
  Отсутствие папки ветки допустимо после успешной проверки repository; ошибки
  listing, неполный ответ и отсутствие доступа не означают пустую ветку.

Восстановите опубликованную Git-историю по предыдущему разделу. В clone агента
должен быть точный BASE_HEAD и вся история, необходимая для полного bundle.
Если агент уже внёс изменения на старой базе, сохраните их и согласуйте с новым
tip средствами Git в своём временном clone до публикации. Не удаляйте чужие
manifests ради выбора tip и не заменяйте пользовательскую историю force/reset.

#### 2. Создать commit и проверенный полный bundle

Прочитайте `AGENTS.md`, внесите изменения и выполните доступные проверки.
Создайте commit только из файлов задачи, используя согласованные данные author.
Ветка должна быть выбранной веткой пользователя; её новый HEAD — потомок BASE_HEAD.
Если HEAD уже совпадает с опубликованным tip, переиспользуйте существующий
Publication ID и переходите к CI/передаче результата, без новой публикации.

Пример в терминале агента; замените значения на проверенные в шаге 1:

```bash
WORK_DIR="/путь/к/рабочему/my-project"
cd "$WORK_DIR"
git status --short
git add path/to/changed-file
git -c core.hooksPath=/dev/null commit -m "Fix requested behavior"

export WORK_DIR
export BRANCH="dev"
export REPOSITORY_ID="REPOSITORY_ID"
export PREVIOUS_PUBLICATION="PREVIOUS_PUBLICATION_ID"
export BASE_HEAD="PREVIOUS_HEAD"
export PUBLICATION_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gdi-publication.XXXXXX")"
```

Для новой пустой ветки задайте `PREVIOUS_PUBLICATION=""` и `BASE_HEAD=""`.
Следующий Python-код запустите в том же окружении. Он ничего не загружает на Drive:
проверяет Git и создаёт локальные bundle, manifest и summary для следующих шагов.
Не удаляйте PUBLICATION_DIR до успешной публикации/сохранения данных для retry.

```python
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

work = Path(os.environ["WORK_DIR"]).resolve()
output = Path(os.environ["PUBLICATION_DIR"]).resolve()
output.mkdir(parents=True, exist_ok=True)
if any(output.iterdir()):
    raise SystemExit("Use an empty preparation directory; preserve existing files for upload retry")
ref = "refs/heads/" + os.environ["BRANCH"]
repository_id = os.environ["REPOSITORY_ID"]
previous = os.environ["PREVIOUS_PUBLICATION"] or None
base_head = os.environ["BASE_HEAD"] or None
if not re.fullmatch(r"[0-9a-f]{32}", repository_id):
    raise SystemExit("Use the verified Repository ID, not a placeholder")
if ((previous is None) != (base_head is None) or
        previous is not None and not re.fullmatch(r"[0-9a-f]{64}", previous) or
        base_head is not None and not re.fullmatch(r"[0-9a-f]{40}", base_head)):
    raise SystemExit("Invalid previous publication/base HEAD")

env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
env["GIT_TERMINAL_PROMPT"] = "0"

def git(*args, cwd=work, allowed=(0,), isolated=False):
    child_env = dict(env)
    if isolated:
        child_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                         GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
        *args], cwd=cwd, env=child_env, capture_output=True, text=True, encoding="utf-8")
    if result.returncode not in allowed:
        raise SystemExit(result.stderr.strip() or "Git validation failed")
    return result.stdout.strip()

git("check-ref-format", ref)
if git("symbolic-ref", "--quiet", "HEAD") != ref:
    raise SystemExit("Switch to the agreed branch before preparing the publication")
if (git("rev-parse", "--is-bare-repository") != "false" or
        git("rev-parse", "--show-object-format") != "sha1" or
        git("rev-parse", "--is-shallow-repository") != "false"):
    raise SystemExit("A full SHA-1 Git worktree is required")
if git("config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$", allowed=(0, 1)):
    raise SystemExit("Partial clones are unsupported")
if git("for-each-ref", "--format=%(refname)", "refs/replace/"):
    raise SystemExit("Replace refs are unsupported")
grafts = Path(git("rev-parse", "--git-path", "info/grafts"))
grafts = grafts if grafts.is_absolute() else work / grafts
if grafts.exists() and grafts.stat().st_size:
    raise SystemExit("Git grafts are unsupported")
head = git("rev-parse", "--verify", ref + "^{commit}")
if head == base_head:
    raise SystemExit("HEAD is already published; reuse PREVIOUS_PUBLICATION")
if base_head is not None:
    git("merge-base", "--is-ancestor", base_head, head)
if git("ls-tree", "-r", "--name-only", head, "--", ".gdi"):
    raise SystemExit("Do not commit local .gdi metadata")
if any(entry.startswith("160000 ") for entry in git("ls-tree", "-r", "-z", head).split("\0")):
    raise SystemExit("Submodule payload is not supported")
if git("grep", "-I", "-l", "-e", "^version https://git-lfs.github.com/spec/v1$",
       head, "--", allowed=(0, 1)):
    raise SystemExit("LFS payload is not supported")

bundle = output / "source.bundle"
git("bundle", "create", str(bundle), ref)
with bundle.open("rb") as handle:
    signature = handle.readline(128)
    if signature not in (b"# v2 git bundle\n", b"# v3 git bundle\n"):
        raise SystemExit("Unsupported bundle signature")
    header_size = 0
    while True:
        line = handle.readline(1024 * 1024 + 1)
        header_size += len(line)
        if not line or header_size > 1024 * 1024 or not line.endswith(b"\n"):
            raise SystemExit("Invalid bundle header")
        if line == b"\n":
            break
        if line.startswith(b"-"):
            raise SystemExit("A full bundle must have no prerequisites")
        if line.startswith(b"@") and (signature != b"# v3 git bundle\n" or
                                      line != b"@object-format=sha1\n"):
            raise SystemExit("Unsupported bundle capability")
if git("bundle", "list-heads", str(bundle)).splitlines() != [head + " " + ref]:
    raise SystemExit("Bundle must advertise exactly the agreed ref and HEAD")

with tempfile.TemporaryDirectory(prefix="gdi-publication-verify-") as quarantine:
    git("init", "--bare", "--object-format=sha1", "--template=", cwd=quarantine, isolated=True)
    git("bundle", "verify", str(bundle), cwd=quarantine, isolated=True)
    git("-c", "fetch.fsckObjects=true", "fetch", "--no-tags", "--no-write-fetch-head",
        "--", str(bundle), "+" + ref + ":refs/heads/incoming", cwd=quarantine, isolated=True)
    if git("rev-parse", "refs/heads/incoming", cwd=quarantine, isolated=True) != head:
        raise SystemExit("Imported HEAD mismatch")
    if git("cat-file", "-t", head, cwd=quarantine, isolated=True) != "commit":
        raise SystemExit("HEAD is not a commit")
    if base_head is not None:
        git("merge-base", "--is-ancestor", base_head, head, cwd=quarantine, isolated=True)
    git("fsck", "--full", "--strict", cwd=quarantine, isolated=True)
if git("rev-parse", "--verify", ref + "^{commit}") != head:
    raise SystemExit("Branch changed while preparing the bundle; retry preparation")

checksum = hashlib.sha256()
with bundle.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        checksum.update(block)
bundle_sha256 = checksum.hexdigest()
bundle_bytes = bundle.stat().st_size
manifest = {"version": 3, "repository_id": repository_id, "ref": ref, "head": head,
    "bundle_sha256": bundle_sha256, "bundle_bytes": bundle_bytes, "bundle_kind": "full",
    "base_publication": None, "base_head": None, "prerequisites": [],
    "previous": previous, "nonce": uuid.uuid4().hex}
raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
publication_id = hashlib.sha256(raw).hexdigest()
branch_directory = ref[len("refs/heads/"):].replace("%", "%25").replace("/", "%2F")
bundle_target = output / (bundle_sha256 + ".bundle")
bundle.rename(bundle_target)
manifest_target = output / (publication_id + ".json")
manifest_target.write_bytes(raw)
summary = {"repository_id": repository_id, "ref": ref, "head": head,
    "previous": previous, "publication_id": publication_id,
    "bundle_sha256": bundle_sha256, "bundle_bytes": bundle_bytes,
    "bundle_file": str(bundle_target), "manifest_file": str(manifest_target),
    "bundle_path": "bundles/" + bundle_target.name,
    "manifest_path": "branches/" + branch_directory + "/" + manifest_target.name}
(output / "publication-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, indent=2))
```

`bundle_kind:"full"`, пустые prerequisites и null base fields обязательны для
этого рецепта. `previous` при этом равен прежнему tip, а не null, если история
публикаций уже есть. Поля manifest не дополняйте временем, именем файла или CI job.

#### 3. Опубликовать bundle, затем manifest

Инструменты Drive должны создавать **обычные файлы** с указанными именами и
точными bytes в существующем проекте. `PROJECT_PATH` ниже — путь внутри общего
root, не имя локального rclone remote. JSON не преобразовывайте в Google Docs.

1. Снова скачайте identity и всю metadata-цепочку ветки. Сверьте Repository ID,
   имена и bytes со снимком шага 1. Если remote изменился, не публикуйте manifest:
   согласуйте новую базу и подготовьте публикацию заново. Проверка одного tip ID
   не обнаруживает повреждение старых manifests под прежними именами.
2. Загрузите `bundle_file` как `PROJECT_PATH/<bundle_path>` из summary. Скачайте
   его обратно, проверьте SHA256 и размер. Если имя уже существует, прочитайте
   существующие bytes: одинаковые можно переиспользовать, отличающиеся — ошибка.
   Не перезаписывайте существующие файлы и не создавайте дубли Drive-имён.
3. Ещё раз проверьте свежую identity и metadata-цепочку перед manifest upload.
   Загрузите `manifest_file` как `PROJECT_PATH/<manifest_path>` **последним**.
   Скачайте его обратно и проверьте SHA256, размер и точное совпадение bytes.
4. Повторно прочитайте всю цепочку, выполните её проверки и убедитесь, что новый
   Publication ID — единственный tip, а HEAD/ref/Repository ID точно ожидаемые.
   Только после этого сообщайте об успешной публикации или отправляйте её на CI.
5. Сохраните summary, manifest bytes и bundle для retry. При потерянном ответе
   upload сначала найдите файлы по именам и проверьте bytes; продолжайте с той же
   nonce и Publication ID. Если ваша публикация уже единственный tip, повторный
   manifest не нужен. Если после неё появился потомок, подтвердите её наличие
   в проверенной цепочке и сообщите, что tip продвинулся; не перезаписывайте его.

Ошибка до manifest оставляет максимум недоступный через GDI orphan bundle.
Повреждённый/частичный manifest, конкурирующее продолжение или смена repository ID
означают отказ, а не успешную передачу. Сохраните данные для диагностики; не удаляйте
произвольные удалённые manifests. Drive не обеспечивает здесь CAS: даже свежие
проверки до/после upload не дают распределённой блокировки, поэтому согласуйте
последовательную запись в одну ветку с пользователем и другими агентами.

#### 4. Уведомить worker или передать публикацию без CI

Для CI используйте следующий раздел с **новыми HEAD и Publication ID**.
Он публикует `request.json`, `request.ready` и `ci_requested` notification;
произвольный commit без manifest worker проверять не должен.

Для обычной передачи без CI можно опубликовать `repository_updated` notification
в общей inbox, как обычный `gdi push`. Оно не запускает CI и не нужно для чтения
публикации через pull. Все поля обязательны, CI-поля равны null:

```json
{
  "inbox_version": 1,
  "event_id": "FIRST_32_HEX_OF_PUBLICATION_ID",
  "type": "repository_updated",
  "repository_id": "REPOSITORY_ID",
  "repository_path": "PROJECT_PATH",
  "ref": "refs/heads/BRANCH",
  "head": "NEW_HEAD",
  "publication_id": "NEW_PUBLICATION_ID",
  "worker_id": null,
  "job_id": null,
  "request_sha256": null
}
```

Сериализуйте через `json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"`,
затем UTF-8. EVENT_SHA256 — SHA256 этих bytes. Загрузите в общий root как
`inbox/EVENT_ID-EVENT_SHA256.json`, после принятой Git-публикации. Для retry сохраняйте
те же bytes/имя; успешно доставленное уведомление заново создавать после его
потребления worker не требуется. Для запуска CI это уведомление не заменяет
`ci_requested` из следующего раздела.

#### 5. Передать пользователю готовые команды

Укажите название подключения пользователя, ветку, точный HEAD и Publication ID,
список изменений и выполненных проверок. При CI также сообщите JOB_ID, profile,
revision и проверенный исход. Название подключения (например, `drive`) существует
только у пользователя; инструменты облачного агента работают с самой папкой Drive.

В существующем подключённом clone на совпадающей ветке и с чистым worktree:

```bash
# Только получить объекты и tracking ref, не меняя текущие файлы:
gdi fetch drive dev
# Получить последний опубликованный tip выбранной текущей ветки без CI gate:
gdi pull drive
# Получить ровно проверенный PASS выбранного задания:
gdi pull drive --passed --job JOB_ID --profile full
```

Выберите подходящую команду; не предлагайте обычный pull как проверенный PASS.
`dev` — пример выбранной ветки; перед pull пользователь должен находиться на ней.
Если tip мог продвинуться после передачи и нужен точный HEAD **без CI**, сначала
`gdi fetch drive dev`, сверка наличия commit и ancestry, затем
`git merge --ff-only NEW_HEAD` на согласованной ветке с чистым деревом.
Dirty/divergent дерево разбирается средствами Git: не предлагайте потерю изменений
через reset/clean. В новом clone сначала подключите уже существующий repository
через `remote add ... --repository-id ...`, без `--init`.

### Отправить существующую публикацию на локальный CI

Для этой операции новый bundle не нужен. Новый commit сначала должен стать
проверенной Git-публикацией по protocol v3; отправка произвольного SHA в CI request
не заменяет публикацию. Не используйте legacy scripts или `ci/queue` для global
worker v2. Ниже полностью описана отправка уже существующей публикации.

1. Скачайте `ci/workers/WORKER_ID/capabilities.json` из общего root.
   Проверьте `ci_version:1`, `inbox_version:1` и worker ID. Возьмите актуальный
   `profile_revision` из `profiles[PROFILE_ID]`; это 64 lowercase hex.
   Не выдумывайте revision и не запускайте второй worker в облаке.
2. Создайте JOB_ID как `uuid.uuid4().hex` (32 lowercase hex) и `created_at` как
   UTC ISO timestamp с часовым поясом. Подготовьте request с точными полями:

```json
{
  "ci_version": 2,
  "job_id": "JOB_ID",
  "repository_id": "REPOSITORY_ID",
  "ref": "refs/heads/BRANCH",
  "head": "HEAD",
  "publication_id": "PUBLICATION_ID",
  "worker_id": "WORKER_ID",
  "profile_id": "PROFILE_ID",
  "profile_revision": "PROFILE_REVISION",
  "created_at": "UTC_TIMESTAMP",
  "retry_of": null,
  "workflow": {
    "path": ".github/workflows",
    "event": "workflow_dispatch",
    "job": "",
    "inputs": {}
  }
}
```

Это шаблон: подставьте проверенные значения, а не буквальные placeholders.
`workflow` выбирает tracked YAML из указанного commit: `path` — файл или каталог,
`job:""` — без выбора отдельного job, `inputs` — строки. Выбирайте событие,
поддерживаемое workflows; `workflow_dispatch` подходит для явного запуска, если
оно объявлено в YAML. Для поведения push используйте `event:"push"`.
При известном GitHub origin добавьте необязательное поле `github_repository`
с `owner/repository` без URL и credentials. Другие поля request не добавляйте.

Все создаваемые JSON сериализуются одинаково:

```python
def encode(obj):
    import json
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
```

3. До upload сохраните локально JOB_ID и точные request bytes. Вычислите
   REQUEST_SHA256 от этих bytes. Сохраните `request.ready` с точными полями
   `{"ci_version":2,"job_id":"JOB_ID","request_sha256":"REQUEST_SHA256"}`.
   Подготовьте canonical bytes уведомления:

```json
{
  "inbox_version": 1,
  "event_id": "JOB_ID",
  "type": "ci_requested",
  "repository_id": "REPOSITORY_ID",
  "repository_path": "PROJECT_PATH",
  "ref": "refs/heads/BRANCH",
  "head": "HEAD",
  "publication_id": "PUBLICATION_ID",
  "worker_id": "WORKER_ID",
  "job_id": "JOB_ID",
  "request_sha256": "REQUEST_SHA256"
}
```

4. Вычислите EVENT_SHA256 от точных bytes уведомления. Загрузите обычные файлы
   строго по порядку, без перезаписи чужих файлов и без преобразования в Docs:

```text
PROJECT_PATH/ci/jobs/JOB_ID/request.json
PROJECT_PATH/ci/jobs/JOB_ID/request.ready
inbox/JOB_ID-EVENT_SHA256.json
```

Уведомление общей inbox публикуется последним. Перед его публикацией убедитесь,
что предыдущие файлы доступны с ожидаемыми bytes. При сетевом сбое допубликовывайте
те же bytes с тем же JOB_ID; не создавайте новый job для повторной загрузки.
Сохраните/сообщите JOB_ID сразу после отправки. Если локальная сессия потеряна,
сначала проверьте ранее отправленный job: новый UUID может вызвать повторный CI.

### Читать прогресс и проверить результат

Путь задания: `PROJECT_PATH/ci/jobs/JOB_ID/`. Периодически читайте `status.json`
и новые `log-chunks/<sequence:08d>-<sha256>.bin`; сохраняйте исходные bytes,
проверяйте hash имени и порядок sequence с 1 без gaps/дубликатов. Отсутствие
`result.json` означает, что терминальный результат ещё не опубликован.
Advisory status и отсутствие прогресса сами по себе не доказывают PASS/FAIL.

После появления `result.json` выполните проверки как клиент GDI:

- Сверьте `ci_version`, job/repository/worker/profile IDs, ref, HEAD,
  Publication ID и profile revision с request; `request_sha256` должен совпасть
  с hash сохранённых request bytes. Проверьте связь request.ready с request.
- Скачайте все объявленные artifacts. Descriptors должны быть уникальными,
  с `complete:true`, ожидаемыми `bytes` и SHA256. Разрешены `build.log`,
  `final-status.json` и безопасные `artifacts/<name>`; log и final status обязательны.
- Сверьте `final-status.json` с result по job ID, run ID, state и request SHA256.
- Конкатенация всех проверенных log chunks должна совпасть с полным `build.log`
  по размеру и SHA256. Если файлы ещё недоступны, повторите чтение, не объявляя PASS.
- PASS требует `exit_code:0`, непустой список stages, хотя бы одну blocking stage
  и `state:"PASS"`/`exit_code:0` у каждой blocking stage. Проверяйте схему и
  terminal states по [docs/ci-protocol.md](docs/ci-protocol.md).

Сообщите точный HEAD, JOB_ID, выбранные workflows/profile/revision, итог CI,
результаты проверок и существенные warnings. При FAIL прочитайте полный log.
Исправления разрешены только в рамках поставленной задачи: новый commit требует
новой Git-публикации, затем нового CI request. Явный повтор завершённого задания
использует новый JOB_ID и `retry_of` со старым ID; timeout ожидания агента не
отменяет работающий CI. Не отправляйте повтор только из-за задержки upload result.

Worker остаётся запущенным после PASS/FAIL. Облачный агент не создаёт `stop.request`,
не останавливает службу и не меняет credentials/host config. Пользователь получает
проверенный commit через свой установленный GDI:

```bash
gdi pull drive --passed --job JOB_ID --profile full
```
