import json
import os
import socket
import ssl
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path


CONFIG_FILE = Path("email_config.json")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError("Файл email_config.json не найден")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def config_value(config: dict, key: str, env_key: str, default=None):
    return config.get(key) or os.environ.get(env_key) or default


def config_bool(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def tcp_probe(host: str, port: int, timeout: int) -> bool:
    print(f"TCP-проверка {host}:{port} ...", end=" ")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            print("OK")
            return True
    except Exception as exc:
        print(f"ERROR: {exc}")
        return False


def print_connection_help(exc: Exception) -> None:
    text = str(exc)
    print("")
    print("Что это обычно значит:")
    if "handshake operation timed out" in text or "timed out" in text:
        print("  - TLS/SSL-соединение блокируется или очень долго не отвечает.")
        print("  - Часто помогает переключение между 465 SSL и 587 STARTTLS.")
        print("  - Иногда исходящий SMTP режет антивирус, корпоративная сеть, Wi-Fi или провайдер.")
    if "Username and Password not accepted" in text or "Application-specific password required" in text:
        print("  - Для Gmail нужен пароль приложения, а не обычный пароль аккаунта.")
    print("")
    print("Что попробовать:")
    print("  1. Если сейчас порт 465: поставьте smtp_port=587, use_tls=true, use_ssl=false.")
    print("  2. Если сейчас порт 587: поставьте smtp_port=465, use_tls=false, use_ssl=true.")
    print("  3. Увеличьте timeout до 90.")
    print("  4. Попробуйте другую сеть, например мобильный интернет.")
    print("  5. Проверьте, не блокирует ли SMTP антивирус или firewall.")


def main() -> int:
    try:
        config = load_config()
    except Exception as exc:
        print(f"ERROR: не удалось загрузить email_config.json: {exc}")
        return 1

    recipients = [email for email in config.get("recipients", []) if email]
    host = config_value(config, "smtp_host", "AI_MONITOR_SMTP_HOST")
    port = int(config_value(config, "smtp_port", "AI_MONITOR_SMTP_PORT", 587))
    username = config_value(config, "smtp_username", "AI_MONITOR_SMTP_USERNAME")
    password = config_value(config, "smtp_password", "AI_MONITOR_SMTP_PASSWORD")
    sender = config_value(config, "from_email", "AI_MONITOR_FROM_EMAIL") or username
    use_tls = config_bool(config, "use_tls", True)
    use_ssl = config_bool(config, "use_ssl", False) or config_bool(config, "smtp_ssl", False)
    timeout = int(config_value(config, "timeout", "AI_MONITOR_SMTP_TIMEOUT", 45))

    missing = []
    if not recipients:
        missing.append("recipients")
    if not host:
        missing.append("smtp_host")
    if not sender:
        missing.append("from_email или smtp_username")
    if not username:
        missing.append("smtp_username")
    if not password:
        missing.append("smtp_password")

    if missing:
        print("ERROR: SMTP-тест невозможен, не заполнены поля:")
        for item in missing:
            print(f"  - {item}")
        return 1

    msg = EmailMessage()
    msg["Subject"] = "AI Regulation Monitor: SMTP test"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        "Тестовое письмо AI Regulation Monitor.\n\n"
        f"Время отправки: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "Если это письмо пришло, SMTP-сервер настроен корректно.\n"
    )

    print(f"Подключение к SMTP: {host}:{port}")
    print(f"Режим: {'SSL' if use_ssl else 'STARTTLS' if use_tls else 'без TLS'}")
    print(f"Таймаут: {timeout} сек.")
    print(f"Отправитель: {sender}")
    print(f"Получатели: {', '.join(recipients)}")

    if not tcp_probe(host, port, timeout):
        print_connection_help(RuntimeError("TCP connection failed"))
        return 1

    try:
        context = ssl.create_default_context()
        if use_ssl:
            smtp_server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            smtp_server = smtplib.SMTP(host, port, timeout=timeout)
        with smtp_server as server:
            server.ehlo()
            if use_tls and not use_ssl:
                server.starttls(context=context)
                server.ehlo()
            server.login(username, password)
            server.send_message(msg)
    except Exception as exc:
        print(f"ERROR: письмо не отправлено: {exc}")
        print_connection_help(exc)
        return 1

    print("OK: тестовое письмо успешно отправлено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
