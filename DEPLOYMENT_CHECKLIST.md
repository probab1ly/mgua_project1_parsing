# Deployment checklist for AI Regulation Monitor

## 1. Что ещё важно закрыть перед хостингом

- Секреты: не выкладывать `.env`, `.ai_monitor_secret`, `private_credentials.txt`, реальные пароли и Gmail app password.
- Git history: старые секреты могли остаться в истории. Перед публичным репозиторием отзовите старые пароли и при необходимости очистите историю через `git filter-repo`.
- SMTP: письма не отправятся, пока не задан `AI_MONITOR_SMTP_PASSWORD`.
- Роли: сейчас любой авторизованный пользователь может запускать обновление данных. Для публичного/боевого режима лучше добавить роли `viewer` и `admin`.
- Фоновый парсинг: weekly scheduler включайте только на одном экземпляре приложения, иначе несколько серверов начнут парсить и рассылать письма одновременно.
- Хранилище: `cache.json`, `update_history/`, `.refresh.lock` лежат на файловой системе. На PaaS с ephemeral disk история может теряться после перезапуска.

## 2. Минимальные переменные окружения

```env
AI_MONITOR_SECRET_KEY=long-random-production-secret-at-least-32-chars
AI_MONITOR_HTTPS=1
AI_MONITOR_PROXY_FIX=1

AI_MONITOR_SMTP_HOST=smtp.gmail.com
AI_MONITOR_SMTP_PORT=587
AI_MONITOR_SMTP_USERNAME=your-address@gmail.com
AI_MONITOR_SMTP_PASSWORD=your-google-app-password
AI_MONITOR_SMTP_TIMEOUT=90

HOST=0.0.0.0
PORT=5000
WAITRESS_THREADS=8

AI_MONITOR_ENABLE_SCHEDULER=0
AI_MONITOR_OPEN_BROWSER=0
```

На Render/Railway/других PaaS обычно `PORT` выставляется автоматически. Не задавайте его вручную, если хостинг сам передаёт порт.

## 3. Команда запуска

```bash
python run_server.py
```

Для платформ с Procfile:

```Procfile
web: python run_server.py
```

## 4. Проверки перед публикацией

```bash
python -m unittest discover -s quality_assurance/tests -v
python -m py_compile app.py scraper.py run_server.py
```

После запуска откройте:

- `/login` — страница входа.
- `/api/status` без входа — должен быть `401`.
- `/robots.txt` — должен запрещать индексацию.
- `/api/email/status` после входа — `ready` должен быть `true`, если нужна рассылка.

## 5. Рекомендованный сценарий для хостинга

1. Создать приватный репозиторий.
2. Убедиться, что `.env`, `.ai_monitor_secret`, `private_credentials.txt` не попали в Git.
3. На хостинге создать web service с командой `python run_server.py`.
4. В панели хостинга добавить переменные окружения из раздела 2.
5. Запустить сервис.
6. Войти на сайт и нажать «Обновить данные».
7. Проверить историю обновлений и email-статус.

## 6. Ограничения текущей версии

- Это файловое приложение без внешней БД. Для одного экземпляра и учебного/внутреннего использования это нормально.
- При нескольких экземплярах нужны общие БД/хранилище и отдельный worker для парсинга.
- Сбор данных обращается к официальным сайтам. Некоторые источники могут отвечать 403/timeout; это нормальный эксплуатационный риск парсеров.
