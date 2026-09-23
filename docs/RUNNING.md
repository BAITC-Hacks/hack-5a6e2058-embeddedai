# Запуск для жюри: Windows, macOS и Linux

Для получения CSV нужны только Python 3.12 и зафиксированные Python-зависимости.
Node.js, браузер, GPU, API-ключи и сервер для расчёта не требуются. `analyze.py` —
тонкая точка входа в тот же пайплайн, который используют CLI и веб-приложение.

## Рекомендуемый путь: uv

Установите [uv по официальной инструкции](https://docs.astral.sh/uv/getting-started/installation/).
Он создаёт отдельное окружение проекта и при необходимости загружает Python 3.12.
Варианты установки:

| ОС | Команда |
|---|---|
| Windows, PowerShell | `winget install --id=astral-sh.uv -e` |
| macOS с Homebrew | `brew install uv` |
| macOS/Linux с curl | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

После установки откройте новый терминал и проверьте `uv --version`.
Склонируйте приватный репозиторий команды или распакуйте его полную копию,
затем перейдите в корень, где находятся `pyproject.toml`, `uv.lock`, `analyze.py`.
Официальный набор уже включён в `data/data/` этого приватного репозитория.

Одна и та же команда работает в PowerShell, bash и zsh:

```text
uv run --frozen --no-dev python analyze.py --data ./data/data --out ./output
```

`uv` выбирает Python по `.python-version`, устанавливает зависимости из `uv.lock`
и запускает расчёт. `--frozen` запрещает незаметное обновление lockfile;
`--no-dev` исключает инструменты разработки из установки. См.
[описание работы с проектами uv](https://docs.astral.sh/uv/guides/projects/).
Интернет нужен при первой загрузке Python и пакетов; сам расчёт сетевых запросов
не выполняет. Не оценивайте скорость алгоритма по времени первой установки.

В `output/` появятся:

- `nodes_roles.csv`: все узлы, роли, поддержка, кластер, приоритет и evidence;
- `clusters.csv`: размер, seed, внутренний оборот, ключевые узлы и гипотеза;
- `top_nodes.csv`: ранжированный список до 50 узлов с объяснениями;
- `run_report.json` и `result.json`: параметры, хеши и состояние расчёта.

Схемы трёх CSV фиксированы по ТЗ. Идентификаторы int64 не преобразуются через
float. CSV и JSON записываются в UTF-8; при открытии CSV в Excel укажите текстовый
тип для `gid`, чтобы Excel не округлил длинные идентификаторы.

## Три отдельных файла

Файлы могут находиться в разных папках и иметь произвольные имена:

```text
uv run --frozen --no-dev python analyze.py --nodes "input/client list.parquet" --edges "input/links.parquet" --transactions "input/payments.parquet" --out "output/result"
```

Используйте либо `--data`, либо все три параметра `--nodes`, `--edges`,
`--transactions`. Смешивание режимов и неполная тройка завершаются ошибкой.
Относительные пути считаются от текущего рабочего каталога. Пути с пробелами
заключайте в кавычки; абсолютные пути также допустимы.

Явно выбранные файлы копируются во временный каталог под стандартными именами,
после чего вызывается обычный пайплайн. Это добавляет время и временное место
для одной копии трёх файлов, зато работает без привилегий на символические ссылки
в Windows. Байты не меняются: SHA-256 в отчёте относится к исходному содержимому.
Временный каталог удаляется и при успехе, и при ошибке. В режиме `--data`
дополнительного копирования нет.

## Другие точки входа

Существующий CLI сохранён:

```text
uv run --frozen --no-dev money-graph analyze --data ./data/data --out ./output
uv run --frozen --no-dev python -m money_graph analyze --data ./data/data --out ./output
```

Оба поддерживают те же параметры отдельных файлов и необязательный
`--rules path/to/rules.json`. Справка:

```text
uv run --frozen --no-dev python analyze.py --help
```

Для независимого синтетического примера:

```text
uv run --frozen --no-dev money-graph demo --out ./var/example-input
uv run --frozen --no-dev python analyze.py --data ./var/example-input --out ./var/example-output
```

Папка `demo --out` должна быть новой или пустой. Папка результата анализа может
быть новой, пустой или содержать только прежние выгрузки приложения. Нельзя
подменять исходные данные, рабочую папку, файл правил, посторонние файлы или
каталог через символическую ссылку. Проверка реальных входных путей выполняется
до копирования во временный каталог.

## Без uv: воспроизводимая установка через pip

Этот путь требует уже установленного **Python 3.12**. `requirements.txt`
экспортирован из `uv.lock`: версии всех runtime-зависимостей зафиксированы,
архивы защищены SHA-256. Используется
[режим проверки хешей pip](https://pip.pypa.io/en/stable/topics/secure-installs/).
`--only-binary=:all:` требует готовые wheels и исключает скрытую сборку C/C++.
На платформе без подходящего wheel установка явно завершится ошибкой.

Windows, PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe analyze.py --data ./data/data --out ./output
```

macOS/Linux:

```sh
python3.12 -m venv .venv
./.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
./.venv/bin/python analyze.py --data ./data/data --out ./output
```

Активация окружения не нужна. `analyze.py` использует код из `src/`, поэтому
после установки этих зависимостей не требуется дополнительный `pip install .`.
Не смешивайте pip-изменения окружения с рекомендуемым uv-процессом: следующая
синхронизация uv вернёт набор пакетов из lockfile.

Для разработчика: обновляйте экспорт вместе с `uv.lock`, а не редактируйте
версии и хеши вручную:

```text
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

## Проверки и веб-интерфейс без bash

Проверка Python, CLI и API без Node.js и браузера:

```text
uv run --frozen python scripts/verify.py --backend-only
```

Выполняются frozen sync, Ruff, проверка форматирования, mypy и pytest.
Для полной проверки дополнительно нужен **Node.js 22.12+ с npm**:

```text
uv run --frozen python scripts/verify.py
```

Она добавляет npm ci, Biome/TypeScript, production build, smoke-test и Playwright.
Chromium загружается при первом прогоне. На Linux при нехватке системных библиотек
выполните из `web/` команду `npx playwright install --with-deps chromium`.

Запуск интерфейса с официальным набором:

```text
uv run --frozen --no-dev python scripts/start.py --data ./data/data --port 3010
```

Откройте `http://127.0.0.1:3010`. `start.py` устанавливает frozen Python-пакеты,
выполняет npm ci и production build, затем запускает сервер. Пути этого launcher
считаются от корня репозитория. Без `--data` запускается синтетический пример.
`scripts/start.sh` и `scripts/verify.sh` сохранены как совместимые обёртки
для пользователей bash; Python-команды выше от bash не зависят.

## Ошибки и границы проверки

| Ситуация | Что делать |
|---|---|
| `uv` не найден | Установить uv и открыть новый терминал |
| Нет доступа к индексам пакетов | Обеспечить интернет/корпоративный proxy для первой установки; затем повторить frozen-команду |
| Python другой версии | Использовать uv или установить Python 3.12 для pip-пути |
| Не найдена зависимость при прямом `python analyze.py` | Выполнить рекомендуемую uv-команду либо pinned pip-установку выше |
| Не найден входной файл | Проверить текущую папку и переданные пути |
| Неверная схема, даты, суммы или ссылки на узлы | Исправить данные по [входному контракту](METHODOLOGY.md#входной-контракт) |
| В папке результата посторонние файлы | Указать отдельную папку через `--out` |
| Не найден Node.js | Для CSV он не нужен; для web/полной проверки установить Node.js 22.12+ с npm |

Успешный расчёт возвращает код 0. Неверные аргументы, входные данные и ошибки
файлов возвращают 2; launchers сохраняют ненулевой код провалившейся команды
и останавливают следующие шаги. Отмена launcher через Ctrl+C возвращает 130.
Ошибка расчёта не заменяет предыдущую успешную выгрузку.

Переносимость не означает, что все комбинации ОС и архитектуры испытаны на этом
ноутбуке. Фактически выполненные прогоны и ограничения указаны в
[VALIDATION_04.md](VALIDATION_04.md); результаты CI следует смотреть отдельно.
