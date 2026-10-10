# Установка и запуск gdi 0.3 (pre-alpha)

Руководство для Linux Mint / Ubuntu / Debian и Bash. Проверено с Python **3.10.12**,
Git 2.34.1 и rclone 1.75.1. Другие версии rclone отдельно не проверялись.
В этой версии работают `remote add/list/remove`, `push`, `fetch`, `pull`, `cache clear`,
ручной `gc`, справка `gdi -h`/`--help` и версия `gdi -v`/`--version` с ASCII-эмблемой.
Реализован автономный CI: job queue, постоянный worker, live progress/console,
проверенные результаты и `pull --passed`. Короткий справочник:
[QUICKSTART.md](QUICKSTART.md). Архитектура: [ARCHITECTURE.md](ARCHITECTURE.md).
Для замеров подключения и обмена включите журнал rclone перед командами:
[docs/profiling.md](docs/profiling.md).

## 1. Что устанавливать

| Компонент | Для чего нужен |
| --- | --- |
| Python 3.10+ | Выполняет gdi |
| Git | Создаёт/проверяет bundles, импортирует commits, делает fast-forward |
| rclone | Передаёт файлы между компьютером и Google Drive |
| python3-venv и pip | Устанавливают gdi в отдельное Python-окружение |
| setuptools и wheel | Собирают Python-пакет при установке |
| systemd user manager | Необязательный постоянный запуск worker на host |

**Дополнительных Python-библиотек для работы и тестов нет.** Не нужны Google SDK,
`google-api-python-client` или pytest.
OAuth и обновление токенов выполняет rclone. Git/rclone — отдельные программы,
они не устанавливаются через pip.

Проверьте доступные версии:

```bash
python3 --version
git --version
rclone version
```

На Mint 21 / Ubuntu 22.04 системный Python обычно уже подходит. Если команда `python3`
указывает на окружение другого проекта, для создания venv можно явно использовать
`/usr/bin/python3.10`. Проверяйте версию выбранного интерпретатора.

Для Debian/Ubuntu/Mint:

```bash
sudo apt update
sudo apt install git python3-venv python3-pip curl unzip ca-certificates
```

Если Python 3.10 установлен отдельно, может понадобиться пакет `python3.10-venv`
из того же источника, откуда установлен Python. Обновлять Python 3.10.12 для gdi не нужно.

### Установка rclone

На Linux native RC transport использует Unix-сокет. Rclone 1.60.1 из пакетов Ubuntu
его не поддерживает; проверенная версия для gdi и CI — 1.75.1. Проверьте
`rclone version` перед использованием установленного rclone. Пакет дистрибутива
подходит, если он содержит версию с поддержкой Unix-сокетов:

```bash
sudo apt install rclone
```

Либо скачать официальный установщик, просмотреть его и выполнить:

```bash
curl -fsSLo /tmp/rclone-install.sh https://rclone.org/install.sh
less /tmp/rclone-install.sh
sudo bash /tmp/rclone-install.sh
rclone version
```

Способы установки и готовые binaries: [официальная инструкция rclone](https://rclone.org/install/).

## 2. Установка Python-пакета

Исходники должны находиться в постоянной папке. Скопируйте проект или клонируйте
его из своего Git remote и выберите ветку `dev`. В начале каждого блока,
обращающегося к исходникам, задайте `INSTALL_DIR` — абсолютный путь к папке gdi.
Замените `/путь/к/gdi` своим путём; кавычки позволяют использовать пробелы в имени.

```bash
INSTALL_DIR="/путь/к/gdi"
cd "$INSTALL_DIR"
python3 -m venv "$HOME/.venvs/gdi"
"$HOME/.venvs/gdi/bin/python" -m pip install --upgrade pip setuptools wheel
"$HOME/.venvs/gdi/bin/python" -m pip install -e .
"$HOME/.venvs/gdi/bin/gdi" --version
"$HOME/.venvs/gdi/bin/gdi" --help
```

`-e .` устанавливает исходники в editable mode: изменения Python-кода сразу видны CLI.
Папку исходников после этого нельзя удалять или перемещать. Для обычной установки
скопированной версии замените последнюю установку на `pip install .`; после обновления
исходников повторите установку. Оба варианта имеют пустой список runtime dependencies.
Build tools при первой установке скачиваются из Python package index.

Не используйте `sudo pip`; устанавливайте gdi в отдельное окружение.
Активировать venv ежедневно не требуется.

### Команда `gdi` в Bash и `.bashrc`

Создайте ссылку на установленный executable:

```bash
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.venvs/gdi/bin/gdi" "$HOME/.local/bin/gdi"
```

Если ссылка уже существует, сначала проверьте `ls -l "$HOME/.local/bin/gdi"`:
повторно создавать её не нужно, если она ведёт в это venv.

Проверьте, находится ли команда после создания ссылки:

```bash
command -v gdi
gdi --version
```

Если команда работает, настройка закончена: `~/.local/bin` уже доступен через
`PATH`, менять `.bashrc` не нужно.

Только если Bash сообщает `gdi: command not found`, проверьте ссылку и наличие
`~/.local/bin` в `PATH`. Если этой папки в `PATH` нет, откройте `~/.bashrc` в
редакторе и добавьте **один раз**:

```bash
# User command line applications, including gdi.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
```

Если меняли `.bashrc`, примените изменения в текущем терминале:

```bash
source "$HOME/.bashrc"
hash -r
command -v gdi
gdi --version
```

Ожидается путь `$HOME/.local/bin/gdi`, затем `gdi 0.3.0` и ASCII-эмблема.
`gdi -v` и `gdi --version` показывают одинаковые версию и эмблему без подписей;
в терминале используется золотой ANSI color. Справка и примеры находятся в
`gdi -h`/`gdi --help`, эти команды тоже эквивалентны.
Для принудительного цвета в pipe: `FORCE_COLOR=1 gdi -v`; `NO_COLOR=1 gdi -v`
выводит обычный текст. Справка и версия не требуют Git-репозитория или доступа к Drive.
Проверьте доступность команды также в новом терминале. Дополнительный alias не
требуется. Для cron/неинтерактивного shell используйте абсолютный путь.

## 3. Google Cloud project и OAuth credentials

Для собственного Google Drive нужен OAuth client типа **Desktop app** и авторизация
Google-пользователя. API key не заменяет OAuth. Для этой инструкции не нужен service
account, `gcloud`, webhook, Pub/Sub или сервер с публичным HTTPS.

Настройка сверена с официальными руководствами 8 октября 2026 года. Названия разделов
консоли могут меняться; старое название `OAuth consent screen` соответствует настройкам
новой **Google Auth Platform**.

### 3.1. Создать project и включить Google Drive API

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Нажмите селектор проекта сверху → **New project**.
3. Назовите проект, например `gdi-personal`; создайте и выберите его.
4. Откройте **APIs & Services → Library**.
5. Найдите именно **Google Drive API**, откройте его и нажмите **Enable**.

Google Cloud Storage и Google Drive — разные продукты; здесь нужен Drive API.
Официальная последовательность: [включение Workspace APIs](https://developers.google.com/workspace/guides/enable-apis).

### 3.2. Consent screen: Branding и Audience

Откройте **Google Auth Platform → Branding → Get started**. Заполните:

- App name: например `gdi rclone personal`.
- User support email: ваш email.
- Audience: **External** для обычного личного Google account.
- Contact information: ваш email; примите необходимые условия и создайте конфигурацию.

Для организации Workspace вариант **Internal** подходит только пользователям этой
организации. В **Audience → Test users → Add users** добавьте аккаунт Google,
чей Drive собираетесь авторизовать. На втором компьютере можно авторизовать тот же
аккаунт отдельно. [Настройка consent screen у Google](https://developers.google.com/workspace/guides/configure-oauth-consent).

### 3.3. Data Access и scope

В **Google Auth Platform → Data Access → Add or remove scopes** добавьте:

```text
https://www.googleapis.com/auth/drive
```

Сохраните изменения. В rclone далее выберите соответствующий scope `drive`.
Это доступ к чтению/записи всех файлов Drive, а не только папки gdi. Для обмена уже
существующими папками между компьютерами здесь используется этот широкий scope.
`root_folder_id` меняет корень навигации rclone, но не сужает OAuth-права.

Google относит `drive` к restricted scopes. Более узкий `drive.file` ограничивает
доступ файлами, доступными приложению, и не является прямой заменой для произвольной
существующей папки. Для передачи обычных `.bundle`/`.json` Google Docs scope не нужен.
Описание прав: [Google Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

### 3.4. Создать Client ID и Client Secret

Откройте **Google Auth Platform → Clients → Create client**:

1. Application type: **Desktop app**.
2. Name: например `rclone-gdi-desktop`.
3. Нажмите **Create**.
4. Сохраните **Client ID** и **Client Secret** в защищённом месте.

ID обычно заканчивается на `.apps.googleusercontent.com`. Не выбирайте Web application:
для Desktop app не требуется вручную добавлять Authorized JavaScript origins.
Credentials JSON скачивать для gdi не обязательно: rclone получает ID/Secret в диалоге.
При желании сохраните JSON вне Git-репозитория.
[Создание Desktop credentials у Google](https://developers.google.com/workspace/guides/create-credentials#desktop-app).

### 3.5. Testing, Production и срок токенов

В External + **Testing** refresh token для Drive истекает через **7 дней**. Это
нормальное ограничение Google. Перевыпуск выполняется через `rclone config reconnect`.
Для постоянного использования откройте **Audience → Publishing status → Publish app**,
чтобы перевести своё приложение в **Production**, затем заново авторизуйте rclone.
Production не делает refresh tokens бессрочными: отзыв доступа и другие условия
Google всё равно могут потребовать повторной авторизации.
[Правила истечения OAuth tokens](https://developers.google.com/identity/protocols/oauth2#expiration).

Если Publish app недоступна, проверьте Branding: консоль может потребовать homepage,
privacy policy и authorized domain. Укажите реальные страницы вашего приложения;
если выполнить требования пока нельзя, можно начать в Testing с повторной авторизацией.
[Замечания rclone о собственном OAuth client](https://rclone.org/drive/#making-your-own-client-id).

Production и verified — разные состояния. Личное приложение может показывать
предупреждение Google о непроверенном приложении; продолжайте только если это ваш
собственный client и консоль разрешает доступ. Для публичного распространения
приложения действуют требования verification, а Workspace-администратор может
дополнительно запрещать доступ. [Требования Google и исключения для личного использования](https://support.google.com/cloud/answer/13464321).

## 4. Настройка rclone для Google Drive

Запустите под своим обычным пользователем:

```bash
rclone config
```

В диалоге выбирайте значения по именам: номера вариантов зависят от версии.

| Вопрос | Ответ |
| --- | --- |
| New remote | `n` |
| name | `gdrive` |
| Storage | `drive` |
| client_id | Ваш Client ID |
| client_secret | Ваш Client Secret |
| scope | `drive` / Full access |
| service_account_file | Пусто, Enter |
| Edit advanced config | `n` |
| Use web browser to automatically authenticate | `y` на компьютере с браузером |
| Configure as Shared Drive | `n` для обычного My Drive |
| Keep this remote | `y` |

В браузере выберите нужный Google account, проверьте имя приложения и подтвердите
доступ. Браузер возвращается на loopback endpoint rclone, обычно порт `53682`.
Сохраните remote и выйдите из диалога (`q`).
Имя `gdrive` — произвольное; далее используйте выбранное имя с двоеточием.
[Официальная настройка Drive backend](https://rclone.org/drive/#configuration).

Проверка:

```bash
rclone listremotes
rclone lsf gdrive: --dirs-only --max-depth 1
rclone config file
```

`listremotes` должен показать `gdrive:`. Пустой listing допустим для пустого Drive;
важно успешное завершение команды. Credentials и refresh token сохраняет rclone.
Путь обычно `~/.config/rclone/rclone.conf`, точный путь показывает
[`rclone config file`](https://rclone.org/commands/rclone_config_file/).
Файл должен быть доступен на запись вашему пользователю для обновления токена.
Не помещайте его в Git и не публикуйте вывод `rclone config show`.

Для стандартного пути можно ограничить доступ:

```bash
chmod 600 "$HOME/.config/rclone/rclone.conf"
```

Если используется другой путь, замените его. Чтобы gdi использовал нестандартный
config, задайте `export RCLONE_CONFIG=/полный/путь/rclone.conf`; gdi наследует rclone
environment. Не запускайте gdi через sudo: у root другой home/config.

### Компьютер без браузера

На headless-компьютере пройдите `rclone config`, выбрав `n` на вопрос об автоматической
авторизации. Rclone покажет команду для компьютера с браузером. Выполните **именно её**
на втором компьютере с rclone (желательно той же версии), авторизуйте нужный account
и перенесите полученный `config_token` обратно в текущий диалог. Используйте тот же
OAuth client. Полученный token — секрет, его нельзя отправлять в публичный чат.

Другой вариант — SSH tunnel:

```bash
ssh -L 53682:127.0.0.1:53682 user@remote-host
```

В этой SSH-сессии запустите `rclone config`, выберите `y` для browser authentication,
а выданный rclone URL откройте в браузере локального компьютера.
[Официальный headless setup](https://rclone.org/remote_setup/).

## 5. Первый проект: компьютер A

Перейдите **в Git-репозиторий, который хотите передавать**. Это может быть любой
поддерживаемый проект, а не обязательно исходники самой gdi.

```bash
cd /путь/к/моему-проекту
git status
git branch --show-current
gdi remote add drive gdrive:gdi/my-project --init
gdi remote list
gdi push drive
```

`--init` используйте только при первом создании dedicated пустой папки. Gdi создаст
её самостоятельно, запишет protocol metadata и выведет постоянный repository ID.
Сохраните ID, чтобы сверить подключение второго компьютера. Старые папки с bundles
не подходят для `--init`; выберите новую папку, например `gdi/my-project`.

`push` передаёт только committed состояние. Если изменения ещё не закоммичены,
сначала создайте commit обычным Git. Незакоммиченные файлы останутся локальными.

Здесь `drive` — **имя gdi remote**, `gdrive:` — **rclone remote**, `gdi/my-project` —
**папка на Google Drive**. Настройки записываются локально в `.gdi/config.json`; GitHub remote
не требуется. На каждый независимый проект выделите другую папку.

## 6. Компьютер B: получить историю и продолжить работу

Установите gdi/rclone на втором компьютере и отдельно авторизуйте доступ к тому же
Drive. Если используются разные Google accounts, у второго должны быть права на
общую папку; адрес в rclone должен вести именно в неё, при необходимости через
advanced `root_folder_id`. Tutorial с одним account проще для первого запуска.

### Уже есть полный clone проекта

```bash
cd /путь/к/clone
gdi remote add drive gdrive:gdi/my-project --repository-id ID_ИЗ_ВЫВОДА_НА_A
gdi fetch drive
git log --oneline --graph --decorate --all -20
git diff HEAD..refs/remotes/drive/main
gdi pull drive
```

Замените `ID_ИЗ_ВЫВОДА_НА_A` на настоящий 32-символьный ID, а `main` на свою ветку.
После fetch текущая ветка и файлы не меняются. После pull HEAD должен совпасть с
опубликованным HEAD. Можно сразу выполнить pull без отдельного fetch.

### Clone пока нет: загрузить в пустой Git repo

На A узнайте имя опубликованной ветки: `git branch --show-current`. Например, это `main`:

```bash
mkdir my-project-copy
cd my-project-copy
git init -b main
gdi remote add drive gdrive:gdi/my-project --repository-id ID_ИЗ_ВЫВОДА_НА_A
gdi pull drive
git log -1 --oneline
```

Для ветки `dev` используйте `git init -b dev`. Gdi не угадывает имя branch.
`remote add` без `--repository-id` тоже работает, но доверяет ID первого подключения.
Повторно выполнять `--init` на B не нужно.

### Обычный цикл работы в обе стороны

```bash
# Получить уже опубликованные изменения перед своей работой.
gdi pull drive
# Изменить файлы и создать commit обычным Git.
git add нужные-файлы
git commit -m "Описание изменения"
gdi push drive
```

На другом компьютере: `gdi pull drive`. Не публикуйте одну ветку с двух компьютеров
одновременно. Push разрешён только fast-forward относительно remote tip; расхождение
разрешайте обычным Git после изучения fetched истории. Gdi сам не выполняет merge/rebase.

Для другой ветки:

```bash
git switch -c feature/example
gdi push drive
# На втором компьютере, не меняя текущую ветку:
gdi fetch drive feature/example
git switch -c feature/example refs/remotes/drive/feature/example
```

Имена со слешами сохраняются. Fetch получает одну указанную/текущую ветку, а не все refs.

### Дельты, контрольные bundles и локальный кеш

Первый push ветки создаёт полный bundle. Следующие push по умолчанию передают только
объекты после предыдущего опубликованного HEAD. Если компьютер пропустил несколько
push, fetch/pull самостоятельно восстанавливает нужную цепочку. Отправитель при этом
не обязан быть включён: все необходимые bundles уже находятся на Drive.

После каждых 20 обновлений от последнего полного bundle создаётся новый полный
контрольный bundle. Поэтому новому компьютеру обычно нужны последний полный bundle
и не более 19 следующих дельт. Изменить частоту для конкретного push:

```bash
gdi push drive --checkpoint-every 10
```

Это параметр текущей команды, он не сохраняется как настройка. Значение должно быть
положительным; `1` включает полные bundles при каждом новом опубликованном HEAD.
Обычный повтор push того же HEAD ничего не загружает. Принудительный полный bundle:

```bash
gdi push drive --full
```

Если последняя публикация — дельта, полный bundle можно создать даже без нового commit.
Если она уже полная и HEAD тот же, повтор ничего не публикует.
Вывод push показывает вид bundle (`full` / `incremental`) и его размер в bytes.

Кеш проверенных объектов находится в
`.gdi/cache/<repository-id>/repository.git` основного worktree. Linked worktrees
используют общие `.gdi`, конфигурацию и lock. Кеш не требует отдельной настройки или службы.
Повторный fetch не скачивает уже проверенные bundles. Metadata цепочки всё ещё читается.

Очистить только локальный кеш:

```bash
gdi cache clear drive
```

Рабочие файлы, refs, настройки и Drive не меняются. Следующий fetch/pull восстановит
кеш из последнего полного bundle и нужных дельт. При ошибке посреди восстановления
полностью проверенные этапы сохраняются; повтор продолжит с них. Обнаруженная потеря
объектов принятой публикации также приводит к автоматическому восстановлению кеша.

Не удаляйте отдельные файлы на Drive вручную: удаление нужной дельты может сломать
восстановление. Для очистки используйте описанный ниже ручной GC. Если дельта потеряна,
клиент с полной локальной историей может опубликовать `--full` на тот же HEAD;
новые получатели начнут с этой контрольной публикации. Без подходящего кеша и без
доступных исходных bundles обычный fetch сообщит ошибку. Принудительный полный push
может восстановить обмен из полного локального repo даже после потери служебного кеша:
новый bundle заново проверяется в пустом временном repo перед загрузкой.

### Очистка старых bundles на Drive

Показать план по **всем опубликованным веткам**:

```bash
gdi gc drive
```

По умолчанию сохраняются два последних полных checkpoint каждой ветки, все bundles
после старшего из них и дополнительные базы дельт. `--keep-checkpoints N` задаёт
положительное количество полных checkpoint. Если checkpoint меньше N, сохраняются
все имеющиеся. Metadata и bundles без manifest не удаляются.

Для применения остановите push/fetch/pull и другие GC на **всех** машинах,
включая агента, host CI и запланированные задачи. Дождитесь завершения текущих
операций. Затем на одной машине:

```bash
gdi gc drive --apply --quiescent
```

`--quiescent` подтверждает, что вы приостановили обмен; программа не может проверить
это на остальных машинах. Без этого флага `--apply` завершается ошибкой. Перед первым
удалением GC скачивает и проверяет все сохраняемые публикации в чистых временных
репозиториях, затем повторно сверяет состояние remote. Обычный dry run только
строит план; bundles для проверки он не скачивает. Исходники и локальный кеш не меняются.

При сетевой ошибке удаления оставьте клиентов приостановленными и повторите команду:
уже удалённые устаревшие bundles не мешают повторному планированию. Возобновите обмен
после успешного завершения. При конфликте или повреждении данных сначала разберите
ошибку, не обходите её ручным удалением manifests.

Для Google Drive rclone по умолчанию отправляет файлы в Корзину (`use_trash=true`).
GC показывает размер удаляемых из рабочей папки bundles, а не изменение квоты Drive.
Корзину и её очистку пользователь контролирует отдельно; gdi не включает безвозвратное
удаление и не очищает Корзину. Полная Git-история остаётся в сохраняемых bundles.
Подробности: [docs/gc.md](docs/gc.md).

## 7. Автономный CI на вашем host

Владелец host выполняет настройку один раз. Агент после этого сам отправляет commits,
читает прогресс/консоль и повторяет исправления до PASS. Ни `pull` в вашем рабочем
каталоге, ни ручная пересылка логов для каждого job не нужны.

### 7.1. Подготовить проект и профиль

На host должен работать rclone с доступом к той же папке. Сверьте:

```bash
INSTALL_DIR="/путь/к/gdi"
gdi remote list
rclone lsf gdrive:gdi/my-project
mkdir -p "$HOME/.config/gdi"
cp "$INSTALL_DIR/examples/worker.json" "$HOME/.config/gdi/worker.json"
```

Откройте `~/.config/gdi/worker.json` в редакторе. Пример использует config version 2:
укажите постоянный `worker_id`, например `user-host`, и `remote_url` общего корня,
например `gdrive:gdi`. Общие retries/timeouts/polling можно оставить по умолчанию.
У разных host одного root должны быть разные worker_id. Фактическое значение
из этого config показывает `gdi worker check --json` (либо с `--config PATH`);
состояние службы проверяется отдельно через `gdi worker status`.
Новые CI-запросы получают этот ID через `--worker`; без него клиент выбирает
свежий совместимый host. Выбор/политика переноса: [docs/scheduling.md](docs/scheduling.md).
Старые capabilities/READY не подтверждают,
что компьютер включён. Паузы polling worker по умолчанию — 30–120 секунд,
timeout исполнения 3600 секунд — отдельная настройка.
Списка проектов, веток и команд CI в этом файле нет.

Установите [act](https://nektosact.com/installation/) и
[Docker Engine](https://docs.docker.com/engine/install/). Проверочная версия act —
v0.2.89. Для systemd act/rclone должны быть в PATH службы либо задайте абсолютный
`act_executable`. Пользователь службы должен иметь доступ к Docker Engine.
Compose нужен только если его вызывает workflow проекта.

Сверьте `act_version` в worker.json с установленной версией и загрузите выбранный
образ перед первым запуском (для default `platforms`):

```bash
act --version
docker pull catthehacker/ubuntu:act-latest
gdi worker check --config ~/.config/gdi/worker.json --runtime --json
```

Worker закрепляет фактический image ID и SHA256 act в execution revision.
Обновление локального образа или бинарника требует restart worker; pending запрос
со старой revision получает REJECTED. Сам worker не устанавливает и не обновляет
инструменты/images. Для независимой проверки используйте [acceptance.md](docs/acceptance.md).

Команды тестов, сборки и анализа читаются из `.github/workflows` проверяемого commit.
Локальное подключение создаёт `gdi remote add drive gdrive:gdi/my-project`;
общий inbox root по умолчанию — `gdrive:gdi`. Для вложенного URL укажите
`--inbox-root gdrive:gdi`. Добавьте `.gdi/` в `.gitignore`; настройки хранятся в
`.gdi/config.json` v2, Git config не изменяется. Старые подключения из Git config
не импортируются; local config v1 требует пересоздания. Полный протокол: [docs/inbox.md](docs/inbox.md).

Workflow path/event/job/inputs выбираются аргументами CI; окружение runner и
secrets настраиваются на host. Полное описание: [docs/worker.md](docs/worker.md).
Legacy config version 1 ещё поддерживается; его pending jobs нужно завершить до
перехода на общую inbox.

```bash
gdi worker check --config ~/.config/gdi/worker.json --json
gdi worker run --config ~/.config/gdi/worker.json
```

Check проверяет схему, run проверяет соединение и публикует capabilities. Сначала
убедитесь, что foreground worker объявил профиль и не сообщает ошибку доступа.
Остановите foreground через Ctrl+C перед запуском службы. Один общий root обслуживается
одним worker; новые проекты подключаются через общую inbox.

### 7.2. Запустить постоянно через systemd

```bash
gdi worker install --config ~/.config/gdi/worker.json
gdi worker start
gdi worker status
gdi worker logs --follow
```

Install создаёт user unit с абсолютными Python/config paths, Start делает enable
и запускает её. После изменения worker.json, окружения или исходников при editable
install выполните `gdi worker restart`: команда перечитывает unit и запускает новый
процесс с актуальными настройками и кодом. При обычной установке сначала обновите пакет.
Установка службы не требует sudo. Для запуска user manager без
входа в систему/после logout владелец host при необходимости включает linger:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Диагностика worker идёт в journalctl: `gdi worker logs` показывает последние
100 сообщений, `gdi worker logs --follow` следит за новыми. `-n 200` задаёт число
сообщений, `--boot` выбирает текущую загрузку системы. Ctrl+C останавливает
просмотр и не останавливает службу. Полная консоль каждого CI сохраняется в
отдельном `build.log` и публикуется через Drive. Агент читает её командами ниже.
После PASS/FAIL служба остаётся работать. Команда `gdi worker stop` запрещает новые
jobs и даёт текущему завершиться; через 120 секунд systemd вправе остановить всю
группу процессов. Лимит меняется override, recovery описан в docs/worker.md.

### 7.3. Агент отправляет commit и видит результат

Из репозитория агента после commit:

```bash
gdi push drive --ci --worker user-host --profile full --json
gdi ci wait drive JOB_ID --follow --timeout 3600 --json
gdi ci logs drive JOB_ID --output /tmp/ci-JOB_ID.log
```

Подставьте job_id из ответа push. JSON stdout содержит точные SHA/IDs и в конце
проверенный terminal result; progress и console wait идут в stderr. При FAIL агент
читает лог, делает исправление новым commit и повторяет push. При сетевой ошибке
upload worker хранит результат локально и повторяет доставку без повторного CI.
Ctrl+C/timeout wait не отменяют job. Явный повтор завершённого CI на прежнем commit:
`gdi ci retry drive JOB_ID --json`.

Явная отмена — `gdi ci cancel drive JOB_ID --json`; она требует поддержки
`cancel_version:1` worker. CANCEL_REQUESTED подтверждает запись на Drive,
проверенный CANCELLED — остановку собственных процессов. Для разрешённой
замены после проверенной отмены используйте
`gdi ci retry drive JOB_ID --worker OTHER_WORKER_ID --json`.
Поздний результат отменённой попытки не принимается как PASS, исходные файлы
остаются историей. Для этой проверки обновите gdi на host и всех клиентах;
новый код worker загружается после restart. Автоматическое переназначение
по таймауту пока не реализовано. Подробный порядок и legacy withdrawal —
[docs/ci.md](docs/ci.md), команды агента с коннектором — [docs/agent.md](docs/agent.md).

### 7.4. Пользователь получает выбранный PASS

На той же ветке, из чистого собственного clone:

```bash
gdi pull drive --passed --job JOB_ID --profile full
```

Применяется SHA выбранного проверенного результата, даже если уже появился новый
непроверенный tip. Только fast-forward; gdi не сбрасывает ваши локальные изменения.
Обычный `gdi pull drive` получает последний tip без CI gate.

Подробный цикл CI через CLI с rclone: [docs/ci.md](docs/ci.md).
Обмен и запрос CI агентом с Drive-коннектором: [docs/agent.md](docs/agent.md).
Практический справочник всех основных действий: [QUICKSTART.md](QUICKSTART.md).
Для GC сначала доставьте все результаты, остановите worker и обмен на всех машинах.
Незавершённые jobs/queue markers блокируют apply-GC; CI logs им не очищаются.

## 8. Проверка без Google Drive

Полный smoke test с настоящим local backend rclone (credentials не нужны):

```bash
export RCLONE_CONFIG_GDILOCAL_TYPE=local
gdi_demo_root=$(mktemp -d /tmp/gdi-demo.XXXXXX)
mkdir "$gdi_demo_root/a" "$gdi_demo_root/b"
cd "$gdi_demo_root/a"
git init -b main
printf 'hello gdi\n' > hello.txt
git add hello.txt
git -c user.name=Demo -c user.email=demo@example.invalid commit -m "First commit"
gdi remote add drive "gdilocal:$gdi_demo_root/transport" --init
gdi push drive
printf 'hello gdi incremental\n' > hello.txt
git add hello.txt
git -c user.name=Demo -c user.email=demo@example.invalid commit -m "Incremental update"
gdi push drive
cd "$gdi_demo_root/b"
git init -b main
gdi remote add drive "gdilocal:$gdi_demo_root/transport"
gdi pull drive
cat hello.txt
git log -1 --oneline
```

Первый push показывает `full`, второй — `incremental`. Ожидается `hello gdi incremental`
и тот же последний commit на A и B. Пример оставляет файлы в уникальном
каталоге `/tmp/gdi-demo.*` для изучения. Для запуска автоматических проверок из исходников:

```bash
INSTALL_DIR="/путь/к/gdi"
cd "$INSTALL_DIR"
"$HOME/.venvs/gdi/bin/python" -m unittest discover -s tests -v
```

## 9. Ошибки и восстановление

| Симптом | Что проверить/сделать |
| --- | --- |
| `gdi: command not found` | `~/.local/bin` в PATH, ссылка, `source ~/.bashrc`, `hash -r`; попробуйте полный путь |
| `No module named gdi` | Устанавливайте пакет тем Python, которым запускаете; для CLI используйте executable из venv |
| `ensurepip is not available` | Установите соответствующий `python3-venv` / `python3.10-venv`, создайте venv заново |
| `rclone not found in PATH` | Установите rclone, проверьте `command -v rclone` |
| `didn't find section in config` | Проверьте имя `gdrive:` и `rclone config file` под тем же пользователем |
| `access_denied` / `403` при авторизации | Проверьте test users, выбранный account, External/Internal и ограничения Workspace |
| `invalid_grant` / token expired | `rclone config reconnect gdrive:`; проверьте Audience/Testing и заново разрешите доступ |
| API disabled / `accessNotConfigured` | Включите Google Drive API именно в проекте OAuth client |
| Папка не видна | Проверьте account, URL, sharing, root_folder_id и scope |
| `--init requires an empty dedicated folder` | Подключайте существующий протокол без `--init`; для нового выберите пустую папку |
| `repository ID mismatch` | Адрес ведёт в другой/заменённый remote. Сверьте `gdi remote list` с ожидаемым ID; не меняйте ID вслепую |
| `no completed publication` | Проверьте имя ветки, дождитесь завершения push на A |
| checksum/ref/HEAD mismatch | Полученные данные не принимаются. Проверьте полный upload и содержимое remote; не отключайте проверки |
| `working tree is dirty` | Сохраните изменения через commit/stash либо разберите untracked files перед pull; fetch разрешён |
| `not a fast-forward` | Изучите `git log --graph --all` и fetched ref; согласуйте историю обычным Git |
| `concurrent push detected` | Сохраните обе истории, согласуйте commit и создайте новый remote; автоматического выбора победителя нет |
| Network timeout | Проверьте сеть и доступность rclone, затем повторите команду |
| `expected protocol v3` | Remote использует старый формат v1/v2; используйте новую пустую папку и новый repository ID |
| Missing bundle / prerequisite mismatch | Проверьте полноту цепочки на Drive; после исправления повторите fetch, при необходимости создайте `push --full` из клиента с проверенной историей |
| Ошибка локального кеша | Выполните `gdi cache clear drive`, затем fetch/pull; remote bundles должны оставаться доступны |

Для диагностики transport сначала выполните `rclone lsf gdrive:gdi/my-project`.
Worker/CI диагностика и recovery: [docs/worker.md](docs/worker.md).
Повторная авторизация документирована в [rclone config reconnect](https://rclone.org/commands/rclone_config_reconnect/).
Операции gdi не должны выполняться одновременно с изменяющими ветку обычными Git-командами.
Ограничения формата репозитория, поведение при частичных uploads и гарантии описаны в
[README.md](README.md) и [протоколе](docs/protocol.md).

## 10. Обновление и удаление

При editable install Python-изменения видны сразу. После изменения packaging metadata
или для обычной установки повторите:

```bash
INSTALL_DIR="/путь/к/gdi"
cd "$INSTALL_DIR"
"$HOME/.venvs/gdi/bin/python" -m pip install -e .
gdi --version
```

### Переход на Git protocol v3

Обратная совместимость с Git exchange v1/v2 и local config v1 отсутствует.
Сохраните полную нужную историю локально; изменение `version` в старой metadata
не преобразует структуру публикаций. На каждом клиенте сохраните старый config
отдельно (если файл есть):

```bash
mv .gdi/config.json .gdi/config.pre-v3.json
```

На первом компьютере выберите новую пустую папку либо прежний URL, если вы уже
удалили старый remote, и создайте новый Repository ID:

```bash
gdi remote add drive gdrive:gdi/my-project-v3 --init
gdi push
gdi status
```

На остальных компьютерах подключитесь к тому же URL без `--init`:

```bash
gdi remote add drive gdrive:gdi/my-project-v3 --repository-id НОВЫЙ_ID_С_ПЕРВОГО_КОМПЬЮТЕРА
gdi fetch
gdi pull
```

Повторите публикацию остальных нужных веток: `gdi push drive BRANCH`.
Старые cache/CI state не удаляются автоматически. Старые версии клиентов не должны
работать с новым remote. Все клиенты и worker должны понимать Git protocol v3.

Перед обновлением worker дождитесь доставки текущих jobs и выполните `gdi worker stop`.
Обновите пакет на всех клиентах, затем `gdi worker start`. После переноса venv или
config повторите `worker install --config ...`, чтобы обновить абсолютные пути unit.
Git protocol v1/v2 remote требует пересоздания по инструкции выше.

Перед удалением worker остановите службу и отключите её автозапуск:

```bash
gdi worker stop
systemctl --user disable gdi-worker.service
```

Не удаляйте state/spool, пока остались недоставленные результаты.

Удалить приложение:

```bash
"$HOME/.venvs/gdi/bin/python" -m pip uninstall gdi
```

После этого можно удалить созданную ссылку `~/.local/bin/gdi`, предварительно проверив
её назначение. Удаление пакета не удаляет Git history, Drive files и rclone credentials.
Удалить только локальное подключение внутри проекта: `gdi remote remove drive`.
Эта команда также сохраняет fetched refs для изучения.
