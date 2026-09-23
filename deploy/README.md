# Деплой на подготовленный Ubuntu VPS

Caddy обслуживает `cca8f159.nip.io`, HTTPS уже настроен. Приложение слушает
`127.0.0.1:3000`. Публикуется только `web/dist`, не исходные файлы и не результаты.

1. Подготовить Python 3.12 и uv. Python должен быть доступен служебному пользователю
   вне `/root`, например `/opt/money-graph-python` через `UV_PYTHON_INSTALL_DIR`.
2. Собрать frontend локально: `npm --prefix web ci && npm --prefix web run build`.
3. Перенести `src/`, `config/`, `web/dist/`, `deploy/`, `pyproject.toml`, `uv.lock`
   и `.python-version` в `/opt/money-graph`. Не включать private/, .env, data/.
4. Выполнить `uv sync --frozen --no-dev --python 3.12` в каталоге приложения.
5. Создать системного пользователя moneygraph без shell, каталог
   `/var/lib/money-graph` с владельцем moneygraph и правами 700.
6. Установить `money-graph.service` в `/etc/systemd/system/`, затем:

```bash
systemctl daemon-reload
systemctl enable --now money-graph
curl -f http://127.0.0.1:3000/health
curl -f https://cca8f159.nip.io/health
```

Сервис запускает синтетический пример; исходные данные организаторов публично
не размещаются. Каталог результатов не находится в web-root.

При обновлении остановить службу, сохранить предыдущий релиз, заменить код и
собранный интерфейс, выполнить `uv sync --frozen --no-dev`, затем запустить и
проверить сценарий. Каталог `/var/lib/money-graph` сохраняется.

Диагностика:

```bash
systemctl status money-graph --no-pager
journalctl -u money-graph -n 60 --no-pager
caddy validate --config /etc/caddy/Caddyfile
```

На подготовленном VPS тело запроса Caddy ограничено
до 32 МБ (`request_body { max_size 32MB }`). API также проверяет лимит каждого
файла и количество записей; обрабатывает один расчёт за раз на процесс.
Это демо без корпоративной авторизации. Публично используйте синтетические
данные, реальные чувствительные сведения анализируйте локально.
