import json
import threading
import logging
import webbrowser
import hashlib
import hmac
import os
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_cors import CORS

from scraper import (
    fetch_all_articles,
    get_stats,
    ALL_SOURCES,
    is_ai_relevant,
    is_allowed_source_url,
    has_explicit_ai_signal,
    has_primary_ai_signal,
    is_official_ai_law_page,
    is_monitoring_relevant,
    parse_date_text,
    is_recent_date,
    legal_priority,
    legal_update_type,
    to_datetime,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("AI_MONITOR_SECRET_KEY", "change-this-secret-key-before-production")
CORS(app)

CACHE_FILE = Path("cache.json")
USERS_FILE = Path("users.json")
EMAIL_CONFIG_FILE = Path("email_config.json")
CACHE_VERSION = 12
CACHE_LOCK = threading.Lock()
WEEKLY_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60

USERS: dict[str, str] = {}

_cache: dict = {
    "articles": [],
    "stats": {},
    "last_updated": None,
    "sources_count": len(ALL_SOURCES),
    "loading": False,
    "diagnostics": [],
    "last_auto_refresh": None,
}


def load_users() -> dict[str, str]:
    if not USERS_FILE.exists():
        logger.error("Файл users.json не найден. Авторизация невозможна.")
        return {}

    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        users = data.get("users", {})
        if not isinstance(users, dict):
            raise ValueError("Поле users должно быть объектом")
        return {
            str(username).strip(): str(password_hash)
            for username, password_hash in users.items()
            if str(username).strip() and str(password_hash).startswith("pbkdf2_sha256$")
        }
    except Exception as e:
        logger.error(f"Ошибка загрузки пользователей из users.json: {e}")
        return {}


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if session.get("user") in USERS:
            return view_func(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication_required"}), 401
        return redirect(url_for("login", next=request.path))

    return wrapped


USERS.update(load_users())


def split_articles_by_mode(articles: list[dict], mode: str) -> list[dict]:
    if mode == "strict":
        return [a for a in articles if a.get("strict_match")]
    if mode == "monitoring":
        return [a for a in articles if not a.get("strict_match")]
    return articles


def article_counts(articles: list[dict]) -> dict:
    strict_count = sum(1 for a in articles if a.get("strict_match"))
    monitoring_count = len(articles) - strict_count
    return {
        "total": len(articles),
        "strict_total": strict_count,
        "monitoring_total": monitoring_count,
    }


def load_email_config() -> dict:
    if not EMAIL_CONFIG_FILE.exists():
        return {"enabled": False, "recipients": []}
    try:
        config = json.loads(EMAIL_CONFIG_FILE.read_text(encoding="utf-8"))
        config.setdefault("enabled", False)
        config.setdefault("recipients", [])
        return config
    except Exception as e:
        logger.error(f"Ошибка загрузки email_config.json: {e}")
        return {"enabled": False, "recipients": []}


def send_email_notification(subject: str, body: str) -> bool:
    config = load_email_config()
    recipients = [r for r in config.get("recipients", []) if r]
    if not config.get("enabled") or not recipients:
        return False

    host = config.get("smtp_host") or os.environ.get("AI_MONITOR_SMTP_HOST")
    port = int(config.get("smtp_port") or os.environ.get("AI_MONITOR_SMTP_PORT", "587"))
    username = config.get("smtp_username") or os.environ.get("AI_MONITOR_SMTP_USERNAME")
    password = config.get("smtp_password") or os.environ.get("AI_MONITOR_SMTP_PASSWORD")
    sender = config.get("from_email") or username
    use_tls = bool(config.get("use_tls", True))
    if not host or not sender:
        logger.warning("Email не отправлен: не указан SMTP host/from_email")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки email: {e}")
        return False


def notify_about_new_articles(previous_links: set[str], articles: list[dict]) -> None:
    new_articles = [a for a in articles if a.get("link") and a.get("link") not in previous_links]
    if not new_articles:
        return
    lines = ["Новые материалы по ИИ и правовому регулированию:", ""]
    for article in new_articles[:25]:
        mode = "строгий режим" if article.get("strict_match") else "расширенный режим"
        lines.extend([
            f"[{mode}] {article.get('title', 'Без заголовка')}",
            f"{article.get('summary', '')[:260]}",
            f"URL: {article.get('link', '')}",
            "",
        ])
    send_email_notification("AI Regulation Monitor: новые материалы", "\n".join(lines))


def save_cache(articles: list, diagnostics: list | None = None) -> None:
    payload = {
        "cache_version": CACHE_VERSION,
        "articles": articles,
        "stats": get_stats(articles),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "sources_count": len(ALL_SOURCES),
        "diagnostics": diagnostics or [],
    }
    with CACHE_LOCK:
        _cache.update(payload)
        _cache["loading"] = False 
    try:
        CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Кэш сохранён на диск")
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша: {e}")


def load_cache_from_disk() -> bool:
    if not CACHE_FILE.exists():
        return False
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if data.get("cache_version") != CACHE_VERSION or data.get("sources_count") != len(ALL_SOURCES):
            logger.info("Кэш устарел после изменения источников. Требуется ручное обновление данных.")
            return False
        articles = []
        for article in data.get("articles", []):
            cached_nlp_ok = article.get("nlp_label") in {"strict", "monitoring"} or article.get("filter_mode") in {"strict", "monitoring"}
            if not is_allowed_source_url(article.get("link", "")):
                continue
            if not cached_nlp_ok and not has_explicit_ai_signal(article.get("title", ""), article.get("summary", "")):
                continue
            if article.get("source") == "Federal Register (официальный API)" and not cached_nlp_ok and not has_primary_ai_signal(article.get("title", ""), article.get("summary", "")):
                continue
            if (
                not cached_nlp_ok
                and not is_monitoring_relevant(article.get("title", ""), article.get("summary", ""))
                and not is_official_ai_law_page(article.get("title", ""), article.get("summary", ""))
            ):
                continue
            if "прямой поиск" in article.get("source", "") and not article.get("collected_at"):
                article["collected_at"] = article.get("date")
                article["date"] = parse_date_text(article.get("title", ""), article.get("summary", ""))
            if not is_recent_date(article.get("date", "")):
                continue
            article.setdefault(
                "legal_update_type",
                legal_update_type(article.get("title", ""), article.get("summary", "")),
            )
            article.setdefault(
                "legal_priority",
                legal_priority(article.get("title", ""), article.get("summary", "")),
            )
            article.setdefault(
                "strict_match",
                is_ai_relevant(article.get("title", ""), article.get("summary", "")),
            )
            article.setdefault("filter_mode", "strict" if article.get("strict_match") else "monitoring")
            article.setdefault("nlp_label", article.get("filter_mode", "monitoring"))
            article.setdefault("nlp_confidence", article.get("analysis_confidence", 0))
            article.setdefault("nlp_reason", "Loaded from cache after relevance validation")
            article.setdefault("analysis_reason", "Материал прошел проверку по связке ИИ + правовой инструмент + правовое действие")
            articles.append(article)
        articles.sort(
            key=lambda a: (
                to_datetime(a.get("date", "")),
                int(a.get("legal_priority", 1)),
                int(a.get("relevance_score", 0)),
            ),
            reverse=True,
        )
        data["articles"] = articles
        data["stats"] = get_stats(articles)
        data["sources_count"] = len(ALL_SOURCES)
        data.setdefault("diagnostics", [])
        with CACHE_LOCK:
            _cache.update(data)
            _cache["loading"] = False
        logger.info(f"Кэш загружен с диска: {len(data.get('articles', []))} статей")
        return True
    except Exception as e:
        logger.error(f"Ошибка загрузки кэша: {e}")
        return False



def refresh_articles(send_notifications: bool = False, auto_refresh: bool = False) -> None:
    logger.info("▶ Начало обновления данных...")
    with CACHE_LOCK:
        _cache["loading"] = True
        previous_links = {a.get("link") for a in _cache.get("articles", []) if a.get("link")}
    try:
        result = fetch_all_articles(include_diagnostics=True)
        articles = result.get("articles", []) if isinstance(result, dict) else result
        diagnostics = result.get("diagnostics", []) if isinstance(result, dict) else []
        save_cache(articles, diagnostics)
        if auto_refresh:
            with CACHE_LOCK:
                _cache["last_auto_refresh"] = datetime.now(timezone.utc).isoformat()
        if send_notifications:
            notify_about_new_articles(previous_links, articles)
        logger.info(f"✅ Обновление завершено: {len(articles)} статей")
    except Exception as e:
        logger.error(f"Ошибка при обновлении: {e}")
        with CACHE_LOCK:
            _cache["loading"] = False


def background_refresh(send_notifications: bool = False, auto_refresh: bool = False):
    t = threading.Thread(
        target=refresh_articles,
        kwargs={"send_notifications": send_notifications, "auto_refresh": auto_refresh},
        daemon=True,
    )
    t.start()


def weekly_refresh_loop() -> None:
    while True:
        time.sleep(WEEKLY_REFRESH_INTERVAL_SECONDS)
        with CACHE_LOCK:
            if _cache.get("loading"):
                logger.info("Еженедельное обновление пропущено: уже идет сбор")
                continue
            _cache["loading"] = True
        refresh_articles(send_notifications=True, auto_refresh=True)


def start_weekly_refresh_thread() -> None:
    thread = threading.Thread(target=weekly_refresh_loop, daemon=True)
    thread.start()



@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("user"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user") in USERS:
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username in USERS and verify_password(password, USERS[username]):
            session.clear()
            session["user"] = username
            return redirect(request.args.get("next") or url_for("index"))
        error = "Неверный логин или пароль"

    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/status")
@login_required
def api_status():
    with CACHE_LOCK:
        articles = list(_cache.get("articles", []))
        counts = article_counts(articles)
        return jsonify({
            "loading": _cache.get("loading", False),
            "last_updated": _cache.get("last_updated"),
            "total": counts["total"],
            "strict_total": counts["strict_total"],
            "monitoring_total": counts["monitoring_total"],
            "sources_count": _cache.get("sources_count", 0),
            "last_auto_refresh": _cache.get("last_auto_refresh"),
        })


@app.route("/api/articles")
@login_required
def api_articles():
    country = request.args.get("country", "")
    category = request.args.get("category", "")
    query = request.args.get("q", "").lower().strip()
    mode = request.args.get("mode", "strict")
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(50, int(request.args.get("per_page", 20)))

    with CACHE_LOCK:
        articles = split_articles_by_mode(list(_cache.get("articles", [])), mode)

    # Фильтрация
    if country:
        articles = [a for a in articles if a.get("country") == country]
    if category:
        articles = [a for a in articles if a.get("category") == category]
    if query:
        articles = [
            a for a in articles
            if query in a.get("title", "").lower()
            or query in a.get("summary", "").lower()
        ]

    total = len(articles)
    start = (page - 1) * per_page
    end = start + per_page
    page_articles = articles[start:end]

    return jsonify({
        "articles": page_articles,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),  # ceil division
    })


@app.route("/api/stats")
@login_required
def api_stats():
    mode = request.args.get("mode", "strict")
    with CACHE_LOCK:
        all_articles = list(_cache.get("articles", []))
        counts = article_counts(all_articles)
        articles = split_articles_by_mode(all_articles, mode)
        stats = get_stats(articles)
        countries = dict(stats.get("countries", {}))
        categories = dict(stats.get("categories", {}))
        for source in ALL_SOURCES:
            countries.setdefault(source["country"], 0)
            categories.setdefault(source["category"], 0)
        stats["countries"] = dict(sorted(countries.items(), key=lambda x: (-x[1], x[0])))
        stats["categories"] = dict(sorted(categories.items(), key=lambda x: (-x[1], x[0])))
        return jsonify({
            "stats": stats,
            "total_all": counts["total"],
            "strict_total": counts["strict_total"],
            "monitoring_total": counts["monitoring_total"],
            "last_updated": _cache.get("last_updated"),
            "loading": _cache.get("loading", False),
        })


@app.route("/api/diagnostics")
@login_required
def api_diagnostics():
    with CACHE_LOCK:
        diagnostics = list(_cache.get("diagnostics", []))
    if not diagnostics:
        diagnostics = [
            {
                "name": s["name"],
                "status": "pending",
                "raw_count": 0,
                "candidate_count": 0,
                "accepted_count": 0,
                "strict_count": 0,
                "monitoring_count": 0,
                "message": "Диагностика появится после обновления данных",
                "url": s.get("url", ""),
            }
            for s in ALL_SOURCES
        ]
    diagnostics.sort(key=lambda item: (item.get("status") == "ok", -int(item.get("accepted_count", 0)), item.get("name", "")))
    return jsonify({"diagnostics": diagnostics})


@app.route("/api/sources")
@login_required
def api_sources():
    return jsonify({
        "sources": [
            {
                "name": s["name"],
                "country": s["country"],
                "flag": s["flag"],
                "category": s["category"],
                "description": s["description"],
            }
            for s in ALL_SOURCES
        ]
    })


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    with CACHE_LOCK:
        if _cache.get("loading"):
            return jsonify({"status": "already_loading"}), 200
        _cache["loading"] = True
    background_refresh()
    return jsonify({"status": "started"})


@app.route("/api/filters")
@login_required
def api_filters():
    with CACHE_LOCK:
        articles = _cache.get("articles", [])
    countries = sorted({s["country"] for s in ALL_SOURCES} | {a["country"] for a in articles})
    categories = sorted({s["category"] for s in ALL_SOURCES} | {a["category"] for a in articles})
    return jsonify({"countries": countries, "categories": categories})



if __name__ == "__main__":
    if not USERS:
        logger.error("Нет доступных учетных записей. Заполните users.json перед запуском.")

    has_cache = load_cache_from_disk()

    if not has_cache:
        logger.info("Кэш не найден. Автоматический сбор отключен — нажмите «Обновить данные» в интерфейсе.")

    start_weekly_refresh_thread()
    threading.Timer(1.5, lambda: webbrowser.open_new_tab("http://127.0.0.1:5000")).start()    
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
