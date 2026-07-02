# Проверка качества

- `tests/` — автоматические unit- и security-тесты.
- `reports/` — полные отчеты QA-аудитов.
- `artifacts/` — JSON-результаты, скриншоты и служебные сценарии проверок.
- `tools/` — отдельные инструменты тестирования, включая проверку SMTP.

Запуск автоматических тестов из корня проекта:

```powershell
python -m unittest discover -s quality_assurance/tests -v
```

Проверка SMTP:

```powershell
python quality_assurance/tools/smtp_test.py
```
