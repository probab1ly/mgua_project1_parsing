# Deployment readiness report

## Текущий статус

Проект можно готовить к размещению на хостинге как закрытое Flask-приложение для ограниченного круга пользователей. Базовые механизмы уже есть: авторизация, CSRF, security headers, закрытые API, noindex/robots, production-запуск через Waitress, шаблон переменных окружения и health-check.

## Что уже подготовлено

- `run_server.py` использует Waitress и читает `HOST`, `PORT`, `WAITRESS_THREADS`.
- `AI_MONITOR_OPEN_BROWSER` теперь выключен по умолчанию.
- `AI_MONITOR_ENABLE_SCHEDULER` теперь выключен по умолчанию, чтобы хостинг сам не запускал еженедельный парсинг без решения владельца.
- Добавлен `AI_MONITOR_PROXY_FIX` для корректной работы HTTPS за reverse proxy.
- Добавлен публичный `/healthz` без утечки данных.
- Добавлен `Procfile` для платформ, которые его поддерживают.
- Обновлён `.env.example`.
- Добавлен `DEPLOYMENT_CHECKLIST.md`.
- Unit-тесты проходят: 17/17.

## Основные пробелы перед реальным production

### 1. Секреты и Git history

Даже если текущие файлы очищены, старые пароли могли остаться в истории Git. Перед публикацией в публичный репозиторий нужно:

- отозвать старый Gmail app password;
- сменить пароли пользователей;
- проверить историю Git;
- при необходимости очистить историю через `git filter-repo`.

### 2. Файловое хранилище

Сейчас данные лежат в файлах:

- `cache.json`;
- `update_history/history.json`;
- `update_history/*.xlsx`;
- `.refresh.lock`;
- `users.json`;
- `email_config.json`.

На простом VPS это нормально. На PaaS с ephemeral disk история и кэш могут пропасть после перезапуска. Для серьёзного production лучше вынести состояние в БД или persistent volume.

### 3. Роли пользователей

Сейчас любой авторизованный пользователь может запускать обновление данных. Для полноценного развёртывания лучше добавить роли:

- `viewer` — только просмотр;
- `operator` — запуск обновления;
- `admin` — управление настройками и пользователями.

### 4. Фоновый парсинг

Парсинг запускается в фоновом потоке внутри web-процесса. Для одного экземпляра это допустимо, но для production лучше:

- отдельный worker;
- очередь задач;
- единый scheduler;
- общий lock в БД/Redis.

Иначе при нескольких экземплярах разные серверы могут одновременно парсить источники и отправлять письма.

### 5. SMTP

`email_config.json` включает Gmail-отправку, но настоящий пароль должен быть только в переменной окружения:

```env
AI_MONITOR_SMTP_PASSWORD=google-app-password
```

Пока переменная не задана, рассылка не будет готова.

### 6. CSP и inline JS/CSS

Security headers есть, но CSP вынужденно содержит `unsafe-inline`, потому что CSS/JS находятся внутри HTML. Для усиления защиты:

- вынести JS в `static/app.js`;
- вынести CSS в `static/styles.css`;
- убрать inline `onclick`;
- убрать `unsafe-inline` из CSP.

## Рекомендованный вариант хостинга

### Лучше всего для текущей архитектуры

VPS или хостинг с persistent disk:

- Timeweb Cloud;
- Selectel;
- Beget VPS;
- любой Ubuntu VPS.

Плюсы: файлы `cache.json` и `update_history` сохраняются, проще контролировать scheduler и SMTP.

### Можно, но осторожно

Render/Railway/Fly.io:

- обязательно проверить persistent disk;
- scheduler включать только на одном сервисе;
- `PORT` брать из переменной платформы.

## Минимальные переменные для хостинга

```env
AI_MONITOR_SECRET_KEY=long-random-secret-at-least-32-chars
AI_MONITOR_HTTPS=1
AI_MONITOR_PROXY_FIX=1

AI_MONITOR_SMTP_HOST=smtp.gmail.com
AI_MONITOR_SMTP_PORT=587
AI_MONITOR_SMTP_USERNAME=your-address@gmail.com
AI_MONITOR_SMTP_PASSWORD=google-app-password
AI_MONITOR_SMTP_TIMEOUT=90

HOST=0.0.0.0
WAITRESS_THREADS=8
AI_MONITOR_OPEN_BROWSER=0
AI_MONITOR_ENABLE_SCHEDULER=0
```

Если хостинг сам задаёт `PORT`, не задавайте его вручную.

## Команда запуска

```bash
python run_server.py
```

## Проверки перед деплоем

```bash
python -m py_compile app.py scraper.py run_server.py
python -m unittest discover -s quality_assurance/tests -v
```

После деплоя:

- `/healthz` должен вернуть `{"status":"ok"}`;
- `/login` должен открываться;
- `/api/status` без входа должен вернуть `401`;
- после входа `/api/email/status` должен показать `ready=true`, если нужна рассылка.

## Итоговая оценка

- Для учебной демонстрации: готово примерно на 85-90%.
- Для закрытого размещения на VPS: готово примерно на 75%.
- Для полноценного production с несколькими пользователями и долгой эксплуатацией: готово примерно на 60-65%, потому что нужны роли, БД/persistent storage и отдельный worker для парсинга.
