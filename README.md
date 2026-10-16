# AI Regulation Monitor

Flask-приложение для мониторинга официальных материалов о правовом регулировании искусственного интеллекта.

## Структура проекта

```text
.
├─ app.py                 # Flask-приложение, API, авторизация, история обновлений
├─ scraper.py             # источники, парсинг, фильтрация, NLP-классификация
├─ run_server.py          # локальный запуск
├─ requirements.txt       # зависимости Python
├─ Procfile               # запуск на хостинге
├─ templates/             # HTML-шаблоны
├─ static/                # статические файлы
├─ data/                  # локальные данные и секреты, не коммитятся
├─ docs/                  # устав, отчеты, инструкции, скриншоты
├─ quality_assurance/     # тесты, QA-отчеты, инструменты проверки
└─ scripts/               # вспомогательные скрипты
```

## Важные файлы

- `data/cache.json` - текущий кэш найденных материалов.
- `data/users.json` - локальные учетные записи.
- `data/email_config.json` - настройки email-рассылки без пароля SMTP.
- `data/update_history/` - история обновлений и Excel-отчеты.
- `docs/Устав проекта AI Regulation Monitor.docx` - актуальный устав проекта.
- `quality_assurance/tests/` - автоматические тесты качества и безопасности.
- `quality_assurance/tools/smtp_test.py` - тест отправки письма через SMTP.

## Запуск

```bash
python run_server.py
```

## Тесты

```bash
python -m unittest discover -s quality_assurance\tests -v
```

## Примечания по безопасности

Файлы `.env`, `data/users.json`, `data/email_config.json`, `data/.ai_monitor_secret`, `data/secrets/`, `data/cache.json` и `data/update_history/` не должны попадать в GitHub. Они уже добавлены в `.gitignore`.
