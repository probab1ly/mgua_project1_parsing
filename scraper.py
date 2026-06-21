import feedparser
from ftfy import fix_text
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import re
import time
import random
import concurrent.futures
import threading
from difflib import SequenceMatcher
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib.parse


# Таймаут для каждого HTTP-запроса (секунды)
REQUEST_TIMEOUT = 12
SLOW_REQUEST_TIMEOUT = 20
HTML_REQUEST_TIMEOUT = 14
SLOW_HOSTS = {
    "www.canada.ca",
    "www.industry.gov.au",
    "www.ag.gov.au",
    "www.oaic.gov.au",
    "www12.senado.leg.br",
}
# Максимум параллельных потоков
MAX_WORKERS = 8
# Общий лимит одного ручного обновления, чтобы приложение не зависало на медленных сайтах.
COLLECT_TIMEOUT = 180
# Берем материалы минимум за последний год, с небольшим запасом для важных
# актов на границе периода (например, начало июня 2025 при проверке в июне 2026).
RECENT_DAYS = 400

STRICT_MODE = "strict"
MONITORING_MODE = "monitoring"
NLP_STRICT_THRESHOLD = 68
NLP_MONITORING_THRESHOLD = 52
FUTURE_DAYS = 365

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

def make_session() -> requests.Session:
    """Сессия с retry и правильными заголовками."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

SESSION = make_session()

SOURCE_DIAGNOSTICS: dict[str, dict] = {}
DIAGNOSTICS_LOCK = threading.Lock()


def reset_source_diagnostics() -> None:
    with DIAGNOSTICS_LOCK:
        SOURCE_DIAGNOSTICS.clear()


def update_source_diagnostic(name: str, **values) -> None:
    with DIAGNOSTICS_LOCK:
        item = SOURCE_DIAGNOSTICS.setdefault(name, {
            "name": name,
            "status": "running",
            "raw_count": 0,
            "candidate_count": 0,
            "accepted_count": 0,
            "strict_count": 0,
            "monitoring_count": 0,
            "rejected_count": 0,
            "message": "",
            "url": "",
        })
        item.update(values)


def source_diagnostics_snapshot() -> list[dict]:
    with DIAGNOSTICS_LOCK:
        return [dict(item) for item in SOURCE_DIAGNOSTICS.values()]


def summarize_article_modes(articles: list[dict]) -> dict:
    strict_count = sum(1 for a in articles if a.get("strict_match"))
    return {
        "accepted_count": len(articles),
        "strict_count": strict_count,
        "monitoring_count": max(0, len(articles) - strict_count),
    }


def format_rejection_summary(raw_count: int, accepted_count: int, reasons: dict[str, int]) -> str:
    if accepted_count:
        return "ok"
    labels = {
        "invalid": "некорректная ссылка/заголовок",
        "external": "ссылка вне разрешенного домена",
        "no_ai": "нет явной темы ИИ",
        "no_legal": "нет связки ИИ с правом",
        "old_date": "дата вне периода мониторинга",
        "unknown_date": "дата не определена",
        "duplicate": "дубликат",
    }
    details = [
        f"{labels.get(key, key)}: {value}"
        for key, value in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
        if value
    ]
    base = f"прочитано {raw_count}, релевантных материалов не найдено"
    return f"{base}; " + "; ".join(details[:4]) if details else base


def final_source_status(name: str) -> str:
    current = SOURCE_DIAGNOSTICS.get(name, {}).get("status")
    return current if current in {"failed", "timeout", "error", "unavailable"} else "ok"

def fix_url(url: str) -> str:
    """Убирает лишние точки и пробелы из URL."""
    if not url:
        return url
    url = url.strip()

    url = re.sub(r'(https?://[^/]+)\./([^\s])', r'\1/\2', url)  # с путём
    url = re.sub(r'(https?://[^/]+)\.$', r'\1', url)             # без пути
    return url


def canonical_url(url: str) -> str:
    """Нормализует URL для дедупликации одинаковых материалов."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(fix_url(url))
        ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
        if parsed.netloc.lower() == "eur-lex.europa.eu":
            ignored.update({"qid", "rid"})
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in ignored
        ]
        return urllib.parse.urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            urllib.parse.urlencode(query),
            "",
        ))
    except Exception:
        return url.strip()


def article_title_key(title: str) -> str:
    text = re.sub(r"\s+", " ", (title or "").lower()).strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return text[:180]


TITLE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "with",
    "new", "latest", "update", "news", "official", "press", "release",
}


def article_title_tokens(title: str) -> set[str]:
    normalized = article_title_key(title)
    tokens = set()
    for token in normalized.split():
        if len(token) <= 2 or token in TITLE_STOPWORDS:
            continue
        if token.endswith("ies") and len(token) > 5:
            token = token[:-3] + "y"
        elif token.endswith("ing") and len(token) > 6:
            token = token[:-3]
        elif token.endswith("ed") and len(token) > 5:
            token = token[:-2]
        elif token.endswith("es") and len(token) > 5:
            token = token[:-2]
        elif token.endswith("s") and len(token) > 4 and not token.endswith("ss"):
            token = token[:-1]
        tokens.add(token)
    return tokens


def document_identifiers(article: dict) -> set[str]:
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    patterns = [
        r"\bcelex[:\s-]*[0-9a-z()]+\b",
        r"\b(?:h\.?r\.?|s\.|bill)\s*\d{1,6}\b",
        r"\b(?:regulation|directive|decision)\s*\(?(?:eu\s*)?\d{4}/\d+\b",
        r"\b(?:act|law|decree|ordinance)\s*(?:no\.?\s*)?\d{1,6}\b",
    ]
    identifiers = set()
    for pattern in patterns:
        identifiers.update(re.findall(pattern, text, flags=re.IGNORECASE))
    return {re.sub(r"\s+", "", value.lower()) for value in identifiers}


def articles_are_duplicate(left: dict, right: dict) -> bool:
    left_url = canonical_url(left.get("link", ""))
    right_url = canonical_url(right.get("link", ""))
    if left_url and left_url == right_url:
        return True

    left_ids = document_identifiers(left)
    right_ids = document_identifiers(right)
    if left_ids and right_ids and left_ids.intersection(right_ids):
        return True

    left_country = left.get("country", "")
    right_country = right.get("country", "")
    if left_country != right_country and "Global" not in {left_country, right_country}:
        return False

    date_distance = abs((to_datetime(left.get("date", "")) - to_datetime(right.get("date", ""))).days)
    if date_distance > 21:
        return False

    left_type = left.get("legal_update_type", "")
    right_type = right.get("legal_update_type", "")
    if left_type and right_type and left_type != right_type:
        return False

    left_key = article_title_key(left.get("title", ""))
    right_key = article_title_key(right.get("title", ""))
    if not left_key or not right_key:
        return False
    sequence_similarity = SequenceMatcher(None, left_key, right_key).ratio()
    left_tokens = article_title_tokens(left_key)
    right_tokens = article_title_tokens(right_key)
    union = left_tokens | right_tokens
    token_similarity = len(left_tokens & right_tokens) / len(union) if union else 0
    containment = (
        min(len(left_key), len(right_key)) >= 35
        and (left_key in right_key or right_key in left_key)
    )
    return sequence_similarity >= 0.84 or token_similarity >= 0.62 or containment


def article_quality(article: dict) -> tuple:
    source = article.get("source", "").lower()
    analytical_source = any(name in source for name in ("iapp", "dataguidance"))
    return (
        0 if analytical_source else 1,
        1 if article.get("strict_match") else 0,
        int(article.get("legal_priority", 1)),
        int(article.get("relevance_score", 0)),
        len(article.get("summary", "")),
    )


def merge_duplicate_articles(primary: dict, duplicate: dict) -> dict:
    if article_quality(duplicate) > article_quality(primary):
        primary, duplicate = duplicate, primary
    sources = set(primary.get("also_reported_by", []))
    sources.add(primary.get("source", ""))
    sources.add(duplicate.get("source", ""))
    links = set(primary.get("alternate_links", []))
    if duplicate.get("link") and duplicate.get("link") != primary.get("link"):
        links.add(duplicate["link"])
    primary["also_reported_by"] = sorted(source for source in sources if source)
    primary["alternate_links"] = sorted(links)
    return primary


def deduplicate_articles(articles: list[dict]) -> list[dict]:
    unique: list[dict] = []
    for article in sorted(articles, key=article_quality, reverse=True):
        duplicate_index = next(
            (index for index, existing in enumerate(unique) if articles_are_duplicate(article, existing)),
            None,
        )
        if duplicate_index is None:
            article.setdefault("also_reported_by", [article.get("source", "")])
            article.setdefault("alternate_links", [])
            unique.append(article)
        else:
            unique[duplicate_index] = merge_duplicate_articles(unique[duplicate_index], article)
    return unique


def is_valid_url(url: str) -> bool:
    if not isinstance(url, str) or not url or len(url) > 4096:
        return False
    if any(ord(char) < 32 or char in {'"', "'", "<", ">", "\\"} for char in url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False
    
# ─────────────────────────────────────────────
# ИСТОЧНИКИ RSS / ATOM
# ─────────────────────────────────────────────
RSS_SOURCES = [
    # ── США ───────────────────────────────────
    {
        "name": "NIST News",
        "url": "https://www.nist.gov/news-events/news/rss.xml",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Стандарты",
        "description": "Национальный институт стандартов и технологий",
    },
    # ── ЯПОНИЯ ────────────────────────────────
    {
        "name": "Japan Digital Agency",
        "url": "https://www.digital.go.jp/rss/news.xml",
        "country": "Japan",
        "flag": "🇯🇵",
        "category": "Госуправление",
        "description": "Новости Digital Agency Japan, включая ИИ в государственном секторе",
    },
    # ── ЕВРОПЕЙСКИЙ СОЮЗ ─────────────────────
    {
        "name": "European Parliament Press Releases",
        "url": "https://www.europarl.europa.eu/rss/doc/press-releases/en.xml",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Законодательство",
        "description": "Официальные пресс-релизы Европейского парламента",
    },
]

TARGETED_FEED_QUERIES = [
    "artificial intelligence regulation",
    "artificial intelligence law",
    "AI Act",
    "AI regulation",
    "algorithmic accountability",
    "automated decision making",
    "generative AI regulation",
    "deepfake regulation",
    "facial recognition regulation",
    "AI liability directive",
    "AI regulatory sandbox",
    "AI code of practice",
    "general-purpose AI obligations",
    "high-risk AI obligations",
]

ALLOWED_SOURCE_DOMAINS = {
    "eur-lex.europa.eu",
    "www.europarl.europa.eu",
    "www.whitehouse.gov",
    "www.ftc.gov",
    "www.nist.gov",
    "www.federalregister.gov",
    "www.consilium.europa.eu",
    "www.edpb.europa.eu",
    "oecd.ai",
    "www.unesco.org",
    "www.gov.uk",
    "cset.georgetown.edu",
    "www.congress.gov",
    "iapp.org",
    "www.iapp.org",
    "www.dataguidance.com",
    "ico.org.uk",
    "digital-strategy.ec.europa.eu",
    "www.canada.ca",
    "www.industry.gov.au",
    "www.minister.industry.gov.au",
    "www.ag.gov.au",
    "www.oaic.gov.au",
    "www.mddi.gov.sg",
    "www.imda.gov.sg",
    "www.pdpc.gov.sg",
    "www.tech.gov.sg",
    "www.digital.go.jp",
    "www8.cao.go.jp",
    "www.meti.go.jp",
    "www.korea.net",
    "www.msit.go.kr",
    "www12.senado.leg.br",
    "www.loc.gov",
}


def hostname(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def is_allowed_source_url(url: str) -> bool:
    if not is_valid_url(url):
        return False
    host = hostname(url)
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_SOURCE_DOMAINS)


def request_timeout_for(url: str) -> int:
    return SLOW_REQUEST_TIMEOUT if hostname(url) in SLOW_HOSTS else REQUEST_TIMEOUT


def html_timeout_for(url: str) -> int:
    return SLOW_REQUEST_TIMEOUT if hostname(url) in SLOW_HOSTS else HTML_REQUEST_TIMEOUT



def response_text(response: requests.Response) -> str:
    declared = (response.encoding or "").lower()
    if not declared or declared in {"iso-8859-1", "latin-1", "windows-1252"}:
        apparent = response.apparent_encoding
        if apparent:
            response.encoding = apparent
    return response.text

def fetch_html_text(url: str, *, allow_browser: bool = False) -> tuple[str, bool]:
    """Загружает HTML с резервным соединением для медленных и блокирующих сайтов."""
    first_error = None
    fallback_headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if hostname(url) in SLOW_HOSTS:
        try:
            resp = requests.get(url, headers=fallback_headers, timeout=(7, SLOW_REQUEST_TIMEOUT))
            resp.raise_for_status()
            return response_text(resp), False
        except Exception as exc:
            if allow_browser:
                dynamic_html = dynamic_html_fallback(url)
                if dynamic_html:
                    return dynamic_html, True
            raise exc

    try:
        resp = SESSION.get(url, timeout=(8, html_timeout_for(url)))
        if resp.status_code not in (403, 429):
            resp.raise_for_status()
            return response_text(resp), False
        first_error = requests.exceptions.HTTPError(response=resp)
    except Exception as exc:
        first_error = exc

    try:
        resp = requests.get(url, headers=fallback_headers, timeout=(10, SLOW_REQUEST_TIMEOUT))
        resp.raise_for_status()
        return response_text(resp), False
    except Exception:
        if allow_browser:
            dynamic_html = dynamic_html_fallback(url)
            if dynamic_html:
                return dynamic_html, True
        if first_error:
            raise first_error
        raise


def is_broad_source(source: dict) -> bool:
    host = hostname(source.get("url", ""))
    broad_hosts = {
        "www.federalregister.gov",
        "www.nist.gov",
        "www.gov.uk",
        "www.canada.ca",
        "www.industry.gov.au",
        "www.ag.gov.au",
        "www.oaic.gov.au",
        "www.meti.go.jp",
        "www.digital.go.jp",
        "www.korea.net",
    }
    return host in broad_hosts or "search" in source.get("url", "").lower()


RSS_SOURCES = [source for source in RSS_SOURCES if is_allowed_source_url(source["url"])]


AI_KEYWORDS = [
    # Основные термины
    "artificial intelligence", "AI regulation", "AI law", "AI act",
    "machine learning regulation", "algorithmic", "algorithm law",
    "generative AI", "large language model", "LLM regulation",
    "autonomous system", "automated decision",
    # Документы и процессы
    "legislation", "bill", "regulation", "directive", "framework",
    "policy", "governance", "compliance", "standard",
    "executive order", "draft law", "proposal", "ordinance",
    # EU специфика
    "EU AI Act", "AI liability", "digital regulation",
    "General Purpose AI", "GPAI", "foundation model",
    # США специфика
    "NIST AI", "FTC AI", "AI safety", "AI risk",
    # Общие
    "deepfake regulation", "facial recognition ban",
    "autonomous weapons", "AI ethics law",
]

AI_KEYWORDS_LOWER = [kw.lower() for kw in AI_KEYWORDS]

AI_SUBJECT_TERMS = [
    "artificial intelligence", "ai", "algorithmic",
    "machine learning", "generative ai", "large language model",
    "llm", "foundation model", "general purpose ai", "gpai",
    "automated decision", "deepfake", "facial recognition",
    "biometric identification", "high-risk ai", "ai system", "ai systems",
    "algorithm", "algorithms", "automated decision-making",
    "agentic ai", "frontier ai", "synthetic media",
]

LEGAL_RELEVANCE_TERMS = [
    "act", "law", "laws", "bill", "legislation", "regulation", "regulations",
    "regulatory", "directive", "framework", "standard", "standards", "compliance", "governance",
    "executive order", "ordinance", "proposal", "draft", "liability",
    "risk management", "safety standard", "enforcement", "rights",
    "accountability", "transparency", "data protection", "privacy",
    "rule", "rules", "notice", "consultation", "guidance", "guidelines",
    "code of practice", "obligations", "requirements", "supervision",
    "oversight", "assessment", "conformity assessment", "certification",
]

STRONG_AI_REGULATION_TERMS = [
    "ai act", "artificial intelligence act", "ai regulation", "ai law",
    "algorithmic accountability act", "automated decision regulation",
    "nist ai risk management framework", "ai risk management framework",
    "deepfake regulation",
    "facial recognition ban", "executive order on ai",
    "biometric identification regulation", "ai liability directive",
    "algorithmic discrimination law", "automated decision-making regulation",
    "general-purpose ai obligations", "high-risk ai obligations",
    "model ai governance framework", "code of practice for ai",
]

ADOPTION_AND_LEGAL_EVENT_TERMS = [
    "adopted", "approved", "passed", "signed", "enacted", "published",
    "entered into force", "enters into force", "takes effect", "effective date",
    "final rule", "proposed rule", "notice of proposed rulemaking",
    "draft bill", "introduced", "reintroduced", "consultation", "call for evidence",
    "implementing regulation", "delegated regulation", "guidelines", "guidance",
    "amended", "amendment", "updated", "issued", "released", "announced",
    "proposal for a regulation", "proposal for a directive", "public consultation",
    "request for comments", "seeks comment", "rulemaking", "legislative proposal",
]

HIGH_VALUE_LEGAL_UPDATE_TERMS = [
    "act", "bill", "law", "regulation", "directive", "ordinance",
    "final rule", "proposed rule", "rulemaking", "notice of proposed rulemaking",
    "proposal for a regulation", "proposal for a directive", "legislative proposal",
    "draft bill", "introduced", "adopted", "approved", "passed", "signed",
    "enacted", "published", "entered into force", "enters into force",
    "amended", "amendment", "implementing regulation", "delegated regulation",
]

LEGAL_INSTRUMENT_TERMS = [
    "act", "law", "bill", "directive", "regulation", "ordinance",
    "executive order", "final rule", "proposed rule", "rulemaking",
    "notice", "guidance", "guidelines", "standard", "standards",
    "code of practice", "framework", "proposal", "draft",
    "consultation", "request for comments", "rfi",
]

LEGAL_ACTION_TERMS = [
    "adopted", "approved", "passed", "signed", "enacted", "published",
    "issued", "released", "announced", "introduced", "reintroduced",
    "amended", "updated", "entered into force", "enters into force",
    "takes effect", "consultation", "request for comments", "seeks comment",
    "proposed", "draft", "final",
]

LEGAL_INTENT_TERMS = [
    "plans", "plan", "planned", "intends", "intention", "considering",
    "considers", "expected", "expects", "could", "may", "might",
    "roadmap", "strategy", "white paper", "green paper", "blueprint",
    "recommendation", "recommendations", "report calls", "calls for",
    "urges", "lawmakers", "ministers", "ministry", "government",
    "regulator", "agency", "parliament", "commission", "committee",
    "working group", "task force", "public hearing", "stakeholder",
    "consulting on", "review", "reviewing", "study", "studying",
    "exploring", "proposal expected", "draft expected",
]

LEGAL_ACTOR_TERMS = [
    "government", "parliament", "congress", "senate", "commission",
    "council", "ministry", "minister", "regulator", "agency",
    "authority", "department", "committee", "lawmakers", "legislators",
    "court", "ombudsman", "data protection authority",
]

AI_SUBJECT_TERMS_LOWER = [term.lower() for term in AI_SUBJECT_TERMS]
LEGAL_RELEVANCE_TERMS_LOWER = [term.lower() for term in LEGAL_RELEVANCE_TERMS]
STRONG_AI_REGULATION_TERMS_LOWER = [term.lower() for term in STRONG_AI_REGULATION_TERMS]
ADOPTION_AND_LEGAL_EVENT_TERMS_LOWER = [term.lower() for term in ADOPTION_AND_LEGAL_EVENT_TERMS]
HIGH_VALUE_LEGAL_UPDATE_TERMS_LOWER = [term.lower() for term in HIGH_VALUE_LEGAL_UPDATE_TERMS]
LEGAL_INSTRUMENT_TERMS_LOWER = [term.lower() for term in LEGAL_INSTRUMENT_TERMS]
LEGAL_ACTION_TERMS_LOWER = [term.lower() for term in LEGAL_ACTION_TERMS]
LEGAL_INTENT_TERMS_LOWER = [term.lower() for term in LEGAL_INTENT_TERMS]
LEGAL_ACTOR_TERMS_LOWER = [term.lower() for term in LEGAL_ACTOR_TERMS]


def has_term(text: str, term: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def has_any_term(text: str, terms: list[str]) -> bool:
    return any(has_term(text, term) for term in terms)


def matched_terms(text: str, terms: list[str]) -> list[str]:
    return sorted({term for term in terms if has_term(text, term)})


def term_positions(text: str, terms: list[str]) -> list[int]:
    positions = []
    for term in terms:
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
        positions.extend(match.start() for match in re.finditer(pattern, text))
    return sorted(positions)


def has_nearby_terms(text: str, left_terms: list[str], right_terms: list[str], max_distance: int = 280) -> bool:
    left_positions = term_positions(text, left_terms)
    right_positions = term_positions(text, right_terms)
    return any(abs(left - right) <= max_distance for left in left_positions for right in right_positions)


def analyze_legal_ai_relevance(title: str, summary: str = "", content: str = "") -> dict:
    """
    Rule-based semantic analyzer.

    The material is accepted only when AI is the object of legal/regulatory action,
    not merely because isolated keywords appear somewhere on the page.
    """
    raw_text = f"{title} {summary} {content}"
    text = re.sub(r"\s+", " ", raw_text.lower()).strip()
    title_text = re.sub(r"\s+", " ", title.lower()).strip()

    ai_terms = matched_terms(text, AI_SUBJECT_TERMS_LOWER)
    strong_terms = matched_terms(text, STRONG_AI_REGULATION_TERMS_LOWER)
    instrument_terms = matched_terms(text, LEGAL_INSTRUMENT_TERMS_LOWER)
    action_terms = matched_terms(text, LEGAL_ACTION_TERMS_LOWER)
    high_value_terms = matched_terms(text, HIGH_VALUE_LEGAL_UPDATE_TERMS_LOWER)

    title_ai_terms = matched_terms(title_text, AI_SUBJECT_TERMS_LOWER)
    title_strong_terms = matched_terms(title_text, STRONG_AI_REGULATION_TERMS_LOWER)
    ai_near_instrument = has_nearby_terms(text, AI_SUBJECT_TERMS_LOWER, LEGAL_INSTRUMENT_TERMS_LOWER)
    ai_near_action = has_nearby_terms(text, AI_SUBJECT_TERMS_LOWER, LEGAL_ACTION_TERMS_LOWER)

    reasons = []
    confidence = 0

    if title_ai_terms:
        confidence += 25
        reasons.append("ИИ явно указан в заголовке")
    elif ai_terms:
        confidence += 15
        reasons.append("ИИ явно указан в описании или тексте")

    if title_strong_terms or strong_terms:
        confidence += 25
        reasons.append("найдена сильная правовая AI-фраза")

    if instrument_terms:
        confidence += 15
        reasons.append("найден правовой инструмент")

    if action_terms:
        confidence += 15
        reasons.append("найдено правовое действие или стадия")

    if high_value_terms:
        confidence += 10
        reasons.append("найден признак законодательной или регуляторной новеллы")

    if ai_near_instrument:
        confidence += 10
        reasons.append("ИИ расположен рядом с правовым инструментом")

    if ai_near_action:
        confidence += 10
        reasons.append("ИИ расположен рядом с правовым действием")

    official_ai_legal_page = bool(ai_terms) and bool(instrument_terms) and (
        bool(title_ai_terms)
        or ai_near_instrument
        or any(term in text for term in ("government", "commission", "parliament", "regulator", "ministry", "agency"))
    )

    if official_ai_legal_page:
        confidence += 10
        reasons.append("официальный правовой материал об ИИ")

    is_relevant = bool(ai_terms) and (
        bool(strong_terms)
        or bool(high_value_terms)
        or (bool(instrument_terms) and bool(action_terms) and (ai_near_instrument or title_ai_terms))
        or official_ai_legal_page
    )

    if not ai_terms:
        reasons.append("отклонено: в самом материале нет явного упоминания ИИ")
    elif not (instrument_terms or strong_terms):
        reasons.append("отклонено: нет правового инструмента")
    elif not (action_terms or high_value_terms or strong_terms or official_ai_legal_page):
        reasons.append("отклонено: нет события принятия, изменения, публикации или рассмотрения")
    elif not is_relevant:
        reasons.append("отклонено: связь между ИИ и правовым событием недостаточно явная")

    return {
        "is_relevant": is_relevant,
        "confidence": min(confidence, 100),
        "ai_terms": ai_terms[:8],
        "strong_terms": strong_terms[:8],
        "legal_instruments": instrument_terms[:8],
        "legal_actions": action_terms[:8],
        "high_value_terms": high_value_terms[:8],
        "reason": "; ".join(reasons),
    }


def classify_legal_ai_nlp(title: str, summary: str = "", content: str = "") -> dict:
    """
    Lightweight local NLP classifier for the second validation stage.

    It does not replace the rule filter. It tokenizes the text, checks legal/AI
    phrase groups, proximity, legal actors and intent signals, then returns a
    reproducible label and confidence score.
    """
    raw_text = f"{title} {summary} {content}"
    text = re.sub(r"\s+", " ", raw_text.lower()).strip()
    title_text = re.sub(r"\s+", " ", title.lower()).strip()

    ai_terms = matched_terms(text, AI_SUBJECT_TERMS_LOWER)
    legal_terms = matched_terms(text, LEGAL_INSTRUMENT_TERMS_LOWER)
    action_terms = matched_terms(text, LEGAL_ACTION_TERMS_LOWER)
    intent_terms = matched_terms(text, LEGAL_INTENT_TERMS_LOWER)
    actor_terms = matched_terms(text, LEGAL_ACTOR_TERMS_LOWER)
    strong_terms = matched_terms(text, STRONG_AI_REGULATION_TERMS_LOWER)
    high_value_terms = matched_terms(text, HIGH_VALUE_LEGAL_UPDATE_TERMS_LOWER)

    title_ai = matched_terms(title_text, AI_SUBJECT_TERMS_LOWER)
    title_legal = matched_terms(title_text, LEGAL_INSTRUMENT_TERMS_LOWER + STRONG_AI_REGULATION_TERMS_LOWER)
    ai_near_legal = has_nearby_terms(text, AI_SUBJECT_TERMS_LOWER, LEGAL_INSTRUMENT_TERMS_LOWER, 320)
    ai_near_action = has_nearby_terms(text, AI_SUBJECT_TERMS_LOWER, LEGAL_ACTION_TERMS_LOWER, 320)
    ai_near_intent = has_nearby_terms(text, AI_SUBJECT_TERMS_LOWER, LEGAL_INTENT_TERMS_LOWER, 360)
    legal_near_intent = has_nearby_terms(text, LEGAL_INSTRUMENT_TERMS_LOWER, LEGAL_INTENT_TERMS_LOWER, 360)

    confidence = 0
    reasons = []

    if title_ai:
        confidence += 20
        reasons.append("AI topic in title")
    elif ai_terms:
        confidence += 12
        reasons.append("AI topic in text")

    if title_legal:
        confidence += 16
        reasons.append("legal term in title")
    elif legal_terms:
        confidence += 12
        reasons.append("legal instrument in text")

    if strong_terms:
        confidence += 24
        reasons.append("strong AI-law phrase")
    if high_value_terms:
        confidence += 14
        reasons.append("formal legal update phrase")
    if action_terms:
        confidence += 12
        reasons.append("legal action or procedural stage")
    if actor_terms:
        confidence += 8
        reasons.append("public/legal actor")
    if intent_terms:
        confidence += 10
        reasons.append("legal-policy intent or plan")
    if ai_near_legal:
        confidence += 12
        reasons.append("AI close to legal instrument")
    if ai_near_action:
        confidence += 10
        reasons.append("AI close to legal action")
    if ai_near_intent and (legal_terms or actor_terms):
        confidence += 8
        reasons.append("AI close to policy intent")
    if legal_near_intent and ai_terms:
        confidence += 6
        reasons.append("legal term close to plan/intent")

    strict_candidate = bool(ai_terms) and (
        bool(strong_terms)
        or (
            bool(high_value_terms)
            and bool(action_terms)
            and (ai_near_legal or title_ai)
        )
        or (
            bool(legal_terms)
            and bool(action_terms)
            and bool(title_ai)
            and (ai_near_legal or ai_near_action)
        )
    )
    monitoring_candidate = bool(ai_terms) and (
        bool(strong_terms)
        or (bool(legal_terms) and (bool(action_terms) or bool(intent_terms) or bool(actor_terms)))
        or (bool(actor_terms) and bool(intent_terms) and (ai_near_intent or ai_near_legal))
    )

    if strict_candidate and confidence >= NLP_STRICT_THRESHOLD:
        label = STRICT_MODE
    elif monitoring_candidate and confidence >= NLP_MONITORING_THRESHOLD:
        label = MONITORING_MODE
    else:
        label = "reject"
        if not ai_terms:
            reasons.append("reject: no explicit AI topic")
        elif not (legal_terms or strong_terms):
            reasons.append("reject: no legal instrument")
        elif not (action_terms or intent_terms or actor_terms or high_value_terms or strong_terms):
            reasons.append("reject: no legal action, actor or policy intent")

    return {
        "label": label,
        "confidence": min(confidence, 100),
        "reason": "; ".join(reasons),
        "ai_terms": ai_terms[:8],
        "legal_terms": legal_terms[:8],
        "action_terms": action_terms[:8],
        "intent_terms": intent_terms[:8],
        "actor_terms": actor_terms[:8],
    }


def is_monitoring_relevant(title: str, summary: str = "") -> bool:
    analysis = analyze_legal_ai_relevance(title, summary)
    nlp = classify_legal_ai_nlp(title, summary)
    return analysis["is_relevant"] or nlp["label"] in {STRICT_MODE, MONITORING_MODE}


def is_low_signal_meeting_title(title: str) -> bool:
    lowered = re.sub(r"\s+", " ", title.lower())
    low_signal = any(term in lowered for term in (
        "meeting notice",
        "public meeting",
        "public meetings",
        "open public event",
        "meeting minutes",
        "council minutes",
        "committee minutes",
    ))
    if not low_signal:
        return False
    explicit_legal = any(has_term(lowered, term) for term in (
        "regulation",
        "regulatory",
        "law",
        "act",
        "bill",
        "rule",
        "legislation",
        "legal",
        "statute",
        "directive",
        "code of practice",
    ))
    return not explicit_legal


def passes_article_filter(title: str, text: str, *, allow_monitoring: bool = True) -> bool:
    if is_low_signal_meeting_title(title):
        return False
    analysis = analyze_legal_ai_relevance(title, text)
    nlp = classify_legal_ai_nlp(title, text)
    if analysis["is_relevant"] and nlp["label"] in {STRICT_MODE, MONITORING_MODE}:
        return True
    return allow_monitoring and nlp["label"] == MONITORING_MODE


def relevance_score(title: str, summary: str = "") -> int:
    analysis = analyze_legal_ai_relevance(title, summary)
    text = re.sub(r"\s+", " ", f"{title} {summary}".lower())
    title_text = title.lower()
    has_ai = has_any_term(text, AI_SUBJECT_TERMS_LOWER)
    has_legal = has_any_term(text, LEGAL_RELEVANCE_TERMS_LOWER)
    has_legal_event = has_any_term(text, ADOPTION_AND_LEGAL_EVENT_TERMS_LOWER)
    has_high_value_update = has_any_term(text, HIGH_VALUE_LEGAL_UPDATE_TERMS_LOWER)
    has_instrument = has_any_term(text, LEGAL_INSTRUMENT_TERMS_LOWER)
    has_action = has_any_term(text, LEGAL_ACTION_TERMS_LOWER)
    strong_matches = sum(1 for term in STRONG_AI_REGULATION_TERMS_LOWER if has_term(text, term))

    score = strong_matches * 3
    if has_ai:
        score += 1
    if has_legal:
        score += 1
    if has_ai and has_legal_event:
        score += 2
    if has_ai and has_high_value_update:
        score += 3
    if has_ai and has_instrument and has_action:
        score += 5
    if has_ai and has_any_term(title_text, LEGAL_RELEVANCE_TERMS_LOWER):
        score += 1
    return max(score, analysis["confidence"])


def is_ai_relevant(title: str, summary: str = "") -> bool:
    """Проверяет, относится ли статья к регулированию ИИ."""
    return analyze_legal_ai_relevance(title, summary)["is_relevant"]


def has_explicit_ai_signal(title: str, summary: str = "") -> bool:
    text = re.sub(r"\s+", " ", f"{title} {summary}".lower())
    return has_any_term(text, AI_SUBJECT_TERMS_LOWER)


def has_primary_ai_signal(title: str, summary: str = "") -> bool:
    text = re.sub(r"\s+", " ", f"{title} {summary}".lower())
    primary_terms = [
        "artificial intelligence", " ai ", "ai-", "generative ai", "machine learning",
        "large language model", "llm", "foundation model", "general purpose ai",
        "gpai", "automated decision", "deepfake", "facial recognition",
        "biometric identification", "high-risk ai", "ai system", "ai systems",
    ]
    padded = f" {text} "
    return any(term in padded for term in primary_terms)


def is_official_ai_law_page(title: str, summary: str = "") -> bool:
    text = re.sub(r"\s+", " ", f"{title} {summary}".lower())
    return has_any_term(text, AI_SUBJECT_TERMS_LOWER) and (
        has_any_term(text, STRONG_AI_REGULATION_TERMS_LOWER)
        or has_any_term(text, LEGAL_INSTRUMENT_TERMS_LOWER)
    )


def legal_priority(title: str, summary: str = "") -> int:
    text = re.sub(r"\s+", " ", f"{title} {summary}".lower())
    if has_any_term(text, [
        "adopted", "approved", "passed", "signed", "enacted",
        "entered into force", "enters into force", "takes effect",
        "final rule",
    ]) and has_any_term(text, LEGAL_INSTRUMENT_TERMS_LOWER):
        return 4
    if has_any_term(text, [
        "proposed rule", "notice of proposed rulemaking", "draft bill",
        "introduced", "reintroduced", "legislative proposal",
        "proposal for a regulation", "proposal for a directive",
    ]):
        return 3
    if has_any_term(text, ["amended", "amendment", "updated"]):
        return 3
    if has_any_term(text, ["guidance", "guidelines", "standard", "standards", "code of practice"]) and has_any_term(text, LEGAL_ACTION_TERMS_LOWER):
        return 2
    if has_any_term(text, STRONG_AI_REGULATION_TERMS_LOWER):
        return 2
    return 1


def legal_update_type(title: str, summary: str = "") -> str:
    text = re.sub(r"\s+", " ", f"{title} {summary}".lower())
    if has_any_term(text, ["entered into force", "enters into force", "takes effect", "effective date"]):
        return "Вступление в силу"
    if has_any_term(text, ["adopted", "approved", "passed", "signed", "enacted"]):
        return "Принятие акта"
    if has_any_term(text, ["final rule", "published", "issued", "released"]):
        return "Публикация акта"
    if has_any_term(text, ["proposed rule", "draft bill", "introduced", "consultation", "request for comments", "seeks comment", "legislative proposal"]):
        return "Проект / рассмотрение"
    if has_any_term(text, ["amended", "amendment", "updated"]):
        return "Изменение акта"
    if has_any_term(text, ["standard", "standards", "guidance", "guidelines", "code of practice"]):
        return "Стандарт / руководство"
    return "Правовое событие"


def build_article(
    *,
    title: str,
    summary: str,
    link: str,
    date: str,
    source: str,
    country: str,
    flag: str,
    category: str,
    source_description: str,
    relevance_text: str = "",
) -> dict:
    full_text = f"{summary} {relevance_text}"
    analysis = analyze_legal_ai_relevance(title, full_text)
    nlp = classify_legal_ai_nlp(title, full_text)
    temporal_status = date_temporal_status(date)
    strict_match = analysis["is_relevant"] and nlp["label"] == STRICT_MODE and temporal_status != "future"
    filter_mode = STRICT_MODE if strict_match else MONITORING_MODE
    return {
        "id": re.sub(r'\W+', '_', link)[:80],
        "title": title,
        "summary": summary[:500] if summary else "Нет описания",
        "link": link,
        "date": date,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "country": country,
        "flag": flag,
        "category": category,
        "source_description": source_description,
        "date_status": temporal_status,
        "relevance_score": relevance_score(title, full_text),
        "legal_update_type": legal_update_type(title, full_text),
        "legal_priority": legal_priority(title, full_text),
        "filter_mode": filter_mode,
        "strict_match": strict_match,
        "nlp_label": nlp["label"],
        "nlp_confidence": nlp["confidence"],
        "nlp_reason": nlp["reason"],
        "analysis_confidence": analysis["confidence"],
        "analysis_reason": analysis["reason"],
        "matched_ai_terms": analysis["ai_terms"],
        "matched_legal_instruments": analysis["legal_instruments"],
        "matched_legal_actions": analysis["legal_actions"],
        "matched_intent_terms": nlp["intent_terms"],
        "matched_legal_actors": nlp["actor_terms"],
    }


def attach_analysis(article: dict, title: str, text: str) -> dict:
    analysis = analyze_legal_ai_relevance(title, text)
    nlp = classify_legal_ai_nlp(title, text)
    temporal_status = date_temporal_status(article.get("date", ""))
    strict_match = analysis["is_relevant"] and nlp["label"] == STRICT_MODE and temporal_status != "future"
    article["filter_mode"] = STRICT_MODE if strict_match else MONITORING_MODE
    article["strict_match"] = strict_match
    article["date_status"] = temporal_status
    article["nlp_label"] = nlp["label"]
    article["nlp_confidence"] = nlp["confidence"]
    article["nlp_reason"] = nlp["reason"]
    article["analysis_confidence"] = analysis["confidence"]
    article["analysis_reason"] = analysis["reason"]
    article["matched_ai_terms"] = analysis["ai_terms"]
    article["matched_legal_instruments"] = analysis["legal_instruments"]
    article["matched_legal_actions"] = analysis["legal_actions"]
    article["matched_intent_terms"] = nlp["intent_terms"]
    article["matched_legal_actors"] = nlp["actor_terms"]
    return article


def parse_date(entry) -> str:
    """Универсальный парсер дат из RSS-записей."""
    for attr in ("published", "updated", "created"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                dt = dateparser.parse(str(raw))
                if dt:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.isoformat()
            except Exception:
                pass
    # Попытка через struct_time
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            try:
                import calendar, time
                ts = calendar.timegm(st)
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


def parse_date_text(*parts: str) -> str:
    """Извлекает реальную дату документа из текста результата поиска."""
    text = " ".join(clean_html(part) for part in parts if part)
    if not text:
        return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()

    patterns = [
        r"\b\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\b",
        r"\b[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                value = match.group(0)
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    dt = datetime.fromisoformat(value)
                elif re.fullmatch(r"[A-Z][a-z]+\s+\d{1,2},\s+\d{4}", value):
                    dt = dateparser.parse(value, dayfirst=False)
                else:
                    dt = dateparser.parse(value, dayfirst=True)
                if dt:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.isoformat()
            except Exception:
                pass

    try:
        dt = dateparser.parse(text, fuzzy=True, dayfirst=True)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
    except Exception:
        pass

    year_match = re.search(r"\b(20\d{2}|19\d{2})\b", text)
    if year_match:
        return datetime(int(year_match.group(1)), 1, 1, tzinfo=timezone.utc).isoformat()

    return datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()


def is_unknown_date(value: str) -> bool:
    try:
        dt = dateparser.parse(value or "")
        if dt:
            return dt.year <= 1970
    except Exception:
        pass
    return True


def to_datetime(value: str) -> datetime:
    try:
        dt = dateparser.parse(value or "")
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except Exception:
        pass
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def is_recent_date(value: str, days: int = RECENT_DAYS) -> bool:
    dt = to_datetime(value)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    upper_bound = datetime.now(timezone.utc) + timedelta(days=FUTURE_DAYS)
    return cutoff <= dt <= upper_bound


def date_temporal_status(value: str) -> str:
    dt = to_datetime(value)
    now = datetime.now(timezone.utc)
    if dt > now + timedelta(days=1):
        return "future"
    if dt < now - timedelta(days=RECENT_DAYS):
        return "old"
    return "current"


def clean_html(text: str) -> str:
    """Удаляет HTML-теги и исправляет типичные ошибки декодирования текста."""
    if not text:
        return ""
    value = str(text)
    if "<" in value or "&" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", fix_text(value)).strip()


def fetch_rss(source: dict) -> list[dict]:
    """Загружает и фильтрует записи из RSS/Atom-ленты."""
    articles = []
    raw_count = 0
    rejected_count = 0
    rejection_reasons: dict[str, int] = {}
    had_error = False
    update_source_diagnostic(source["name"], status="running", url=source.get("url", ""))
    try:
        if not is_allowed_source_url(source["url"]):
            logger.warning(f"[{source['name']}] Источник не входит в белый список доменов, пропуск")
            update_source_diagnostic(source["name"], status="failed", message="domain is not in allowlist")
            return []

        resp = SESSION.get(source["url"], timeout=request_timeout_for(source["url"]))
        if resp.status_code in (403, 404, 410):
            logger.warning(f"[{source['name']}] Недоступен (HTTP {resp.status_code}), пропуск")
            update_source_diagnostic(source["name"], status="unavailable", message=f"HTTP {resp.status_code}: source feed is unavailable or blocked")
            return []
        resp.raise_for_status()

        feed = feedparser.parse(resp.content)
        raw_count = len(feed.entries)
        update_source_diagnostic(source["name"], raw_count=raw_count, candidate_count=raw_count)
        logger.info(f"[{source['name']}] Получено записей: {len(feed.entries)}")

        if feed.bozo and not feed.entries:
            logger.warning(f"[{source['name']}] Пустая или битая лента, пропуск")
            update_source_diagnostic(source["name"], status="unavailable", message="empty or broken feed")
            return []
        
        for entry in feed.entries:
            title = clean_html(getattr(entry, "title", ""))
            summary = clean_html(
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
            )
            raw_link = getattr(entry, "link", "")
            link = fix_url(raw_link)

            if not title or not link or not is_valid_url(link):
                rejected_count += 1
                rejection_reasons["invalid"] = rejection_reasons.get("invalid", 0) + 1
                continue
            if not is_allowed_source_url(link):
                rejected_count += 1
                rejection_reasons["external"] = rejection_reasons.get("external", 0) + 1
                continue
            if is_broad_source(source) and not has_primary_ai_signal(title, summary):
                rejected_count += 1
                rejection_reasons["no_ai"] = rejection_reasons.get("no_ai", 0) + 1
                continue
            if not has_explicit_ai_signal(title, summary):
                rejected_count += 1
                rejection_reasons["no_ai"] = rejection_reasons.get("no_ai", 0) + 1
                continue
            if not passes_article_filter(title, summary):
                rejected_count += 1
                rejection_reasons["no_legal"] = rejection_reasons.get("no_legal", 0) + 1
                continue
            date = parse_date(entry)
            if not is_recent_date(date):
                rejected_count += 1
                rejection_reasons["old_date"] = rejection_reasons.get("old_date", 0) + 1
                continue

            article = {
                "id": re.sub(r'\W+', '_', link)[:80],
                "title": title,
                "summary": summary[:500] if summary else "Нет описания",
                "link": link,
                "date": date,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "source": source["name"],
                "country": source["country"],
                "flag": source["flag"],
                "category": source["category"],
                "source_description": source["description"],
                "relevance_score": relevance_score(title, summary),
                "legal_update_type": legal_update_type(title, summary),
                "legal_priority": legal_priority(title, summary),
            }
            articles.append(attach_analysis(article, title, summary))

    except Exception as e:
        had_error = True
        if isinstance(e, (requests.Timeout, requests.exceptions.ReadTimeout)):
            logger.warning(f"[{source['name']}] Источник не ответил за отведенное время, пропуск: {source['url']}")
            update_source_diagnostic(source["name"], status="timeout", message="request timeout")
        else:
            logger.error(f"[{source['name']}] Ошибка: {e}")
            update_source_diagnostic(source["name"], status="error", message=str(e)[:220])

    if had_error:
        return articles
    mode_summary = summarize_article_modes(articles)
    update_source_diagnostic(
        source["name"],
        status=final_source_status(source["name"]),
        raw_count=raw_count,
        candidate_count=raw_count,
        rejected_count=rejected_count,
        message=format_rejection_summary(raw_count, len(articles), rejection_reasons),
        **mode_summary,
    )
    return articles


# ─────────────────────────────────────────────
# СПЕЦИАЛЬНЫЕ СКРАПЕРЫ
# ─────────────────────────────────────────────

def scrape_eurlex_ai() -> list[dict]:
    """Скрапит EUR-Lex по точным запросам о правовом регулировании ИИ."""
    articles = []
    queries = [
        "artificial intelligence",
        "\"artificial intelligence act\"",
        "\"AI Act\"",
        "\"high-risk AI\"",
        "\"general-purpose AI\"",
        "\"biometric identification\" artificial intelligence",
        "\"automated decision-making\"",
    ]

    for query in queries:
        try:
            url = (
                "https://eur-lex.europa.eu/search.html"
                f"?scope=EURLEX&text={urllib.parse.quote_plus(query)}"
                "&lang=en&type=quick&qid=1"
            )
            resp = SESSION.get(url, timeout=request_timeout_for(url))
            time.sleep(random.uniform(0.7, 1.6))
            soup = BeautifulSoup(response_text(resp), "html.parser")

            results = soup.select(".SearchResult")[:8]
            for r in results:
                title_tag = r.select_one("a.title")
                date_tag = r.select_one(".date")
                desc_tag = r.select_one(".snippet")

                if not title_tag:
                    continue

                title = clean_html(title_tag.get_text())
                link = fix_url("https://eur-lex.europa.eu" + title_tag.get("href", ""))
                date_str = clean_html(date_tag.get_text()) if date_tag else ""
                summary = clean_html(desc_tag.get_text()) if desc_tag else ""
                relevance_text = summary
                if not is_valid_url(link) or not is_allowed_source_url(link):
                    continue
                if not has_explicit_ai_signal(title, relevance_text):
                    continue
                if not passes_article_filter(title, relevance_text) and not is_official_ai_law_page(title, relevance_text):
                    continue
                date = parse_date_text(date_str, r.get_text(" "), title, summary)
                if not is_recent_date(date):
                    continue

                article = {
                    "id": re.sub(r'\W+', '_', link)[:80],
                    "title": title,
                    "summary": (summary or f"Результат EUR-Lex по запросу: {query}")[:500],
                    "link": link,
                    "date": date,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "source": "EUR-Lex (прямой поиск)",
                    "country": "EU",
                    "flag": "🇪🇺",
                    "category": "Законодательство",
                    "source_description": "Официальный реестр законодательства ЕС",
                    "relevance_score": relevance_score(title, relevance_text),
                    "legal_update_type": legal_update_type(title, relevance_text),
                    "legal_priority": legal_priority(title, relevance_text),
                }
                articles.append(attach_analysis(article, title, relevance_text))
        except Exception as e:
            logger.error(f"[EUR-Lex direct: {query}] Ошибка: {e}")
    return articles


def scrape_congress_ai() -> list[dict]:
    """Скрапит Congress.gov — законопроекты США по ИИ."""
    articles = []
    queries = [
        "artificial intelligence",
        "AI regulation",
        "algorithmic accountability",
        "automated decision systems",
        "deepfake artificial intelligence",
    ]

    for query in queries:
        try:
            search = urllib.parse.quote_plus(query)
            url = (
                "https://www.congress.gov/search"
                f"?q=%7B%22source%22%3A%22legislation%22%2C%22search%22%3A%22{search}%22%7D"
                "&pageSize=10"
            )
            resp = SESSION.get(url, timeout=request_timeout_for(url))
            time.sleep(random.uniform(0.7, 1.6))
            soup = BeautifulSoup(response_text(resp), "html.parser")

            for item in soup.select("li.expanded")[:10]:
                title_tag = item.select_one("span.result-heading a")
                date_tag = item.select_one("span.result-item .date")
                desc_tag = item.select_one("p.result-item__value")

                if not title_tag:
                    continue

                title = clean_html(title_tag.get_text())
                link = "https://www.congress.gov" + title_tag.get("href", "")
                summary = clean_html(desc_tag.get_text()) if desc_tag else ""
                relevance_text = summary
                if not is_valid_url(link) or not is_allowed_source_url(link):
                    continue
                if not has_explicit_ai_signal(title, relevance_text):
                    continue
                if not passes_article_filter(title, relevance_text) and not is_official_ai_law_page(title, relevance_text):
                    continue
                date = parse_date_text(date_tag.get_text() if date_tag else "", item.get_text(" "), title, summary)
                if not is_recent_date(date):
                    continue

                article = {
                    "id": re.sub(r'\W+', '_', link)[:80],
                    "title": title,
                    "summary": (summary or f"Результат Congress.gov по запросу: {query}")[:500],
                    "link": link,
                    "date": date,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "source": "US Congress (прямой поиск)",
                    "country": "USA",
                    "flag": "🇺🇸",
                    "category": "Законопроекты",
                    "source_description": "Законодательная база Конгресса США",
                    "relevance_score": relevance_score(title, relevance_text),
                    "legal_update_type": legal_update_type(title, relevance_text),
                    "legal_priority": legal_priority(title, relevance_text),
                }
                articles.append(attach_analysis(article, title, relevance_text))
        except Exception as e:
            logger.error(f"[Congress.gov: {query}] Ошибка: {e}")
    return articles


def scrape_federal_register_ai() -> list[dict]:
    """Берет документы Federal Register минимум за последний год через официальный API."""
    articles = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).date().isoformat()
    seen = set()

    for query in TARGETED_FEED_QUERIES:
        try:
            params = {
                "conditions[term]": query,
                "conditions[publication_date][gte]": cutoff,
                "order": "newest",
                "per_page": 100,
            }
            resp = SESSION.get(
                "https://www.federalregister.gov/api/v1/documents.json",
                params=params,
                timeout=request_timeout_for("https://www.federalregister.gov"),
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", []):
                link = fix_url(item.get("html_url", ""))
                if not link or link in seen or not is_allowed_source_url(link):
                    continue
                title = clean_html(item.get("title", ""))
                summary = clean_html(item.get("abstract", "") or item.get("type", ""))
                document_type = clean_html(item.get("type", ""))
                date = parse_date_text(item.get("publication_date", ""))
                relevance_text = f"{summary} {document_type}"

                if not title or not is_recent_date(date):
                    continue
                if not has_primary_ai_signal(title, ""):
                    continue
                if not has_explicit_ai_signal(title, relevance_text):
                    continue
                if not passes_article_filter(title, relevance_text):
                    continue

                seen.add(link)
                article = {
                    "id": re.sub(r'\W+', '_', link)[:80],
                    "title": title,
                    "summary": (summary or f"Federal Register: {document_type}")[:500],
                    "link": link,
                    "date": date,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "source": "Federal Register (официальный API)",
                    "country": "USA",
                    "flag": "🇺🇸",
                    "category": "Регулятор",
                    "source_description": "Официальные правила, уведомления и proposed rules США",
                    "relevance_score": relevance_score(title, relevance_text),
                    "legal_update_type": legal_update_type(title, relevance_text),
                    "legal_priority": legal_priority(title, relevance_text),
                }
                articles.append(attach_analysis(article, title, relevance_text))
        except Exception as e:
            logger.error(f"[Federal Register API: {query}] Ошибка: {e}")

    return articles


HTML_SEARCH_SOURCES = [
    {
        "name": "UK GOV – AI Regulation Search",
        "url": "https://www.gov.uk/search/all?keywords=artificial%20intelligence%20regulation&order=updated-newest",
        "country": "UK",
        "flag": "🇬🇧",
        "category": "Законодательство",
        "description": "Официальный поиск GOV.UK по регулированию искусственного интеллекта",
        "base_url": "https://www.gov.uk",
        "selectors": ["li.gem-c-document-list__item"],
        "fetch_details": True,
        "max_detail_pages": 15,
    },
    {
        "name": "European Data Protection Board News",
        "url": "https://www.edpb.europa.eu/news/news_en",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Регулятор",
        "description": "Официальные новости EDPB, включая guidance и решения по ИИ и защите данных",
        "base_url": "https://www.edpb.europa.eu",
        "selectors": [".views-row"],
        "fetch_details": True,
        "max_detail_pages": 12,
    },
    {
        "name": "USA FTC – AI Press Releases",
        "url": "https://www.ftc.gov/news-events/news/press-releases?search=artificial%20intelligence",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Регулятор",
        "description": "Официальные пресс-релизы FTC по ИИ, алгоритмам и правоприменению",
        "base_url": "https://www.ftc.gov",
        "selectors": [".node--view-mode-search-result", ".views-row"],
        "link_selector": ".node-title a",
        "fetch_details": True,
        "max_detail_pages": 15,
    },
    {
        "name": "DataGuidance – Artificial Intelligence",
        "url": "https://www.dataguidance.com/topics/artificial-intelligence",
        "country": "Global",
        "flag": "🌐",
        "category": "Правовая аналитика",
        "description": "Профильные новости о регулировании ИИ и защите данных",
        "base_url": "https://www.dataguidance.com",
    },
    {
        "name": "IAPP – AI Governance Search",
        "url": "https://iapp.org/news/?q=artificial%20intelligence%20regulation",
        "country": "Global",
        "flag": "🌐",
        "category": "Правовая аналитика",
        "description": "Новости IAPP о privacy, AI governance и регулировании",
        "base_url": "https://iapp.org",
    },
    {
        "name": "ICO – AI News and Blogs",
        "url": "https://ico.org.uk/about-the-ico/media-centre/news-and-blogs/",
        "country": "UK",
        "flag": "🇬🇧",
        "category": "Регулятор",
        "description": "Материалы британского ICO по ИИ, privacy и guidance",
        "base_url": "https://ico.org.uk",
    },
    {
        "name": "European Commission Digital Strategy – AI",
        "url": "https://digital-strategy.ec.europa.eu/en/search?query=artificial%20intelligence%20act",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Еврокомиссия",
        "description": "Новости и публикации Еврокомиссии по цифровому регулированию и ИИ",
        "base_url": "https://digital-strategy.ec.europa.eu",
    },
    {
        "name": "Singapore MDDI – AI",
        "url": "https://www.mddi.gov.sg/search?query=artificial%20intelligence",
        "country": "Singapore",
        "flag": "🇸🇬",
        "category": "Госуправление",
        "description": "Ministry of Digital Development and Information: AI policy news",
        "base_url": "https://www.mddi.gov.sg",
    },
    {
        "name": "Japan Digital Agency – AI",
        "url": "https://www.digital.go.jp/en/search?q=artificial%20intelligence",
        "country": "Japan",
        "flag": "🇯🇵",
        "category": "Госуправление",
        "description": "Digital Agency Japan: AI in government and digital policy",
        "base_url": "https://www.digital.go.jp",
    },
]

DIRECT_SEARCH_SOURCES = [
    {
        "name": "EUR-Lex (прямой поиск)",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Законодательство",
        "description": "Прямой поиск по правовой базе EUR-Lex",
    },
    {
        "name": "US Congress (прямой поиск)",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Законопроекты",
        "description": "Прямой поиск по законопроектам Congress.gov",
    },
    {
        "name": "Federal Register (официальный API)",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Регулятор",
        "description": "Официальный API Federal Register",
    },
]

OFFICIAL_PAGE_SOURCES = [
    {
        "name": "EU AI Act Official Page",
        "url": "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Законодательство",
        "description": "European Commission official AI Act page with latest implementation news and legal materials",
    },
    {
        "name": "EU AI Act Governance and Enforcement",
        "url": "https://digital-strategy.ec.europa.eu/en/policies/ai-act-governance-and-enforcement",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Регулятор",
        "description": "European Commission page on AI Act governance, enforcement, AI Office and competent authorities",
        "date": "2026-06-01",
    },
    {
        "name": "EU AI Act Service Desk",
        "url": "https://digital-strategy.ec.europa.eu/en/news/commission-launches-ai-act-service-desk-and-single-information-platform-support-ai-act",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Регулятор",
        "description": "European Commission launch of the AI Act Service Desk and Single Information Platform",
        "date": "2025-10-08",
    },
    {
        "name": "EU AI Act Guidelines",
        "url": "https://digital-strategy.ec.europa.eu/en/news/supporting-implementation-ai-act-clear-guidelines",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Руководства",
        "description": "European Commission guidelines for practical implementation of the AI Act",
        "date": "2025-12-04",
    },
    {
        "name": "EU AI-Generated Content Code",
        "url": "https://digital-strategy.ec.europa.eu/en/news/commission-launches-work-code-practice-marking-and-labelling-ai-generated-content",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Руководства",
        "description": "European Commission work on code of practice for marking and labelling AI-generated content under the AI Act",
        "date": "2025-11-05",
    },
    {
        "name": "EU AI Pact Progress",
        "url": "https://digital-strategy.ec.europa.eu/en/news/ai-pact-marks-one-year-progress-trustworthy-ai-europe",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Регулятор",
        "description": "European Commission update on AI Pact and preparation for AI Act compliance",
        "date": "2025-12-15",
    },
    {
        "name": "EU GPAI Safety Tender",
        "url": "https://digital-strategy.ec.europa.eu/en/funding/eu-ai-office-launches-eu9-million-tender-technical-support-gpai-safety",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Регулятор",
        "description": "EU AI Office tender for technical support on enforcement of GPAI systemic-risk obligations under the AI Act",
        "date": "2025-07-10",
    },
    {
        "name": "ICO AI Innovation Response",
        "url": "https://ico.org.uk/about-the-ico/media-centre/news-and-blogs/2026/05/ico-response-to-government-on-safe-ai-powered-innovation/",
        "country": "UK",
        "flag": "🇬🇧",
        "category": "Регулятор",
        "description": "Official regulator response about proposed AI regulation, legal requirements and regulatory certainty",
        "date": "2026-05-29",
    },
    {
        "name": "ICO AI and Biometrics Strategy",
        "url": "https://ico.org.uk/about-the-ico/our-information/our-strategies-and-plans/artificial-intelligence-and-biometrics-strategy/what-we-have-achieved-so-far-on-ai-and-biometrics/",
        "country": "UK",
        "flag": "🇬🇧",
        "category": "Регулятор",
        "description": "ICO AI and biometrics strategy: supervision, guidance and data protection law for AI",
        "date": "2025-06-05",
    },
    {
        "name": "UNESCO Artificial Intelligence",
        "url": "https://www.unesco.org/en/artificial-intelligence",
        "country": "UNESCO",
        "flag": "🌐",
        "category": "Международные стандарты",
        "description": "UNESCO official AI page covering AI ethics, policy and international governance materials",
    },
    {
        "name": "Canada AI Portal",
        "url": "https://www.canada.ca/en/services/science/innovation/artificial-intelligence.html",
        "country": "Canada",
        "flag": "🇨🇦",
        "category": "Госуправление",
        "description": "Официальный портал Канады по AI policy, AIDA и responsible AI",
    },
    {
        "name": "Canada Responsible AI",
        "url": "https://www.canada.ca/en/government/system/digital-government/digital-government-innovations/responsible-use-ai.html",
        "country": "Canada",
        "flag": "🇨🇦",
        "category": "Руководства",
        "description": "Responsible use of artificial intelligence in Government of Canada",
    },
    {
        "name": "Australia National AI Plan",
        "url": "https://www.industry.gov.au/publications/national-ai-plan",
        "country": "Australia",
        "flag": "🇦🇺",
        "category": "Госуправление",
        "description": "National AI Plan and regulatory frameworks for AI harms",
    },
    {
        "name": "Australia Attorney-General AI Transparency",
        "url": "https://www.ag.gov.au/about-us/accountability-and-reporting/attorney-generals-department-artificial-intelligence-transparency-statement",
        "country": "Australia",
        "flag": "🇦🇺",
        "category": "Руководства",
        "description": "AI transparency statement aligned with responsible use of AI in government",
    },
    {
        "name": "Singapore Agentic AI Framework",
        "url": "https://www.imda.gov.sg/resources/press-releases-factsheets-and-speeches/press-releases/2026/new-model-ai-governance-framework-for-agentic-ai",
        "country": "Singapore",
        "flag": "🇸🇬",
        "category": "Руководства",
        "description": "Model AI Governance Framework for Agentic AI",
        "date": "2026-01-22",
        "browser_fallback": True,
        "use_config_fallback": True,
    },
    {
        "name": "Singapore AI Governance Framework",
        "url": "https://www.pdpc.gov.sg/help-and-resources/2020/01/model-ai-governance-framework",
        "country": "Singapore",
        "flag": "🇸🇬",
        "category": "Руководства",
        "description": "Singapore approach to AI governance",
        "browser_fallback": True,
    },
    {
        "name": "Japan Cabinet Office AI Act",
        "url": "https://www8.cao.go.jp/cstp/ai/ai_act/ai_act.html",
        "country": "Japan",
        "flag": "🇯🇵",
        "category": "Госуправление",
        "description": "Cabinet Office Japan: AI Act materials, legal text and official AI policy documents",
        "title": "Japan Cabinet Office: Artificial Intelligence Act",
        "date": "2025-06-04",
    },
    {
        "name": "South Korea AI Basic Act",
        "url": "https://www.korea.net/NewsFocus/policies/view?articleId=286183",
        "country": "South Korea",
        "flag": "🇰🇷",
        "category": "Законодательство",
        "description": "Official Korea.net policy news on AI Basic Act",
        "date": "2026-01-22",
        "use_config_fallback": True,
    },
    {
        "name": "Brazil AI Bill",
        "url": "https://www.loc.gov/item/global-legal-monitor/2025-05-23/brazil-senate-advances-discussions-on-bill-to-regulate-ai-use/",
        "country": "Brazil",
        "flag": "🇧🇷",
        "category": "Законопроекты",
        "description": "Brazil Senate advances discussions on bill to regulate AI use",
        "date": "2025-05-23",
        "browser_fallback": True,
        "use_config_fallback": True,
    },
]

ALL_SOURCES = RSS_SOURCES + HTML_SEARCH_SOURCES + DIRECT_SEARCH_SOURCES + OFFICIAL_PAGE_SOURCES


def extract_page_date(soup: BeautifulSoup, *fallback_parts: str) -> str:
    selectors = [
        "meta[property='article:published_time']",
        "meta[property='article:modified_time']",
        "meta[name='date']",
        "meta[name='dcterms.date']",
        "meta[name='dc.date']",
        "meta[name='pubdate']",
        "time[datetime]",
    ]
    parts = []
    for selector in selectors:
        for tag in soup.select(selector):
            value = tag.get("content") or tag.get("datetime") or tag.get_text(" ")
            if value:
                parts.append(value)
    parts.extend(fallback_parts)
    return parse_date_text(*parts)


def html_candidate_blocks(soup: BeautifulSoup, selectors: list[str] | None = None) -> list:
    if selectors:
        for selector in selectors:
            blocks = soup.select(selector)
            if blocks:
                return blocks
        return []
    selectors = [
        "article",
        "li",
        ".search-result",
        ".result",
        ".views-row",
        ".card",
        ".listing-item",
        ".news-item",
        ".teaser",
    ]
    blocks = []
    for selector in selectors:
        blocks.extend(soup.select(selector))
    return blocks or soup.select("a[href]")


def best_link_from_block(block, base_url: str, link_selector: str = "") -> tuple[str, str]:
    link_tag = block if getattr(block, "name", "") == "a" else None
    if link_tag is None and link_selector:
        link_tag = block.select_one(link_selector)
    if link_tag is None:
        link_tag = block.select_one("a[href]")
    if not link_tag:
        return "", ""
    title = clean_html(link_tag.get_text(" "))
    if not title:
        title = clean_html(block.get_text(" "))[:220]
    href = link_tag.get("href", "")
    link = fix_url(urllib.parse.urljoin(base_url, href))
    return title, link


def fetch_detail_context(url: str) -> tuple[str, str]:
    """Загружает страницу материала и возвращает основной текст и дату."""
    try:
        resp = SESSION.get(url, timeout=html_timeout_for(url))
        resp.raise_for_status()
        soup = BeautifulSoup(response_text(resp), "html.parser")
        description = soup.select_one(
            "meta[name='description'], meta[property='og:description'], meta[name='twitter:description']"
        )
        description_text = clean_html(description.get("content", "")) if description else ""
        content_nodes = soup.select(
            "main p, main li, article p, article li, "
            ".field--name-body p, .field--name-body li, .gem-c-govspeak p, .gem-c-govspeak li"
        )
        body_text = clean_html(" ".join(node.get_text(" ") for node in content_nodes))
        detail_text = clean_html(f"{description_text} {body_text}")[:12000]
        return detail_text, extract_page_date(soup, description_text, body_text[:2000])
    except Exception as exc:
        logger.debug(f"[detail] Не удалось загрузить {url}: {exc}")
        return "", datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()


def dynamic_html_fallback(url: str) -> str:
    """
    Optional browser fallback for JavaScript-heavy sources.

    The project keeps Playwright optional: if it is not installed, diagnostics
    will show that the static parser was used only.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=15000)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        logger.warning(f"[dynamic_html_fallback] Browser fallback failed for {url}: {e}")
        return ""


def scrape_html_search_source(source: dict) -> list[dict]:
    """Собирает ссылки с профильных правовых страниц без RSS."""
    articles = []
    seen = set()
    candidate_count = 0
    rejected_count = 0
    rejection_reasons: dict[str, int] = {}
    detail_pages_loaded = 0
    used_browser_fallback = False
    had_error = False
    update_source_diagnostic(source["name"], status="running", url=source.get("url", ""))
    try:
        logger.info(f"[{source['name']}] HTML-поиск: {source['url']}")
        html, request_used_browser = fetch_html_text(source["url"])
        used_browser_fallback = request_used_browser
        soup = BeautifulSoup(html, "html.parser")

        blocks = html_candidate_blocks(soup, source.get("selectors"))
        if len(blocks) <= 3:
            dynamic_html = dynamic_html_fallback(source["url"])
            if dynamic_html:
                used_browser_fallback = True
                soup = BeautifulSoup(dynamic_html, "html.parser")
                blocks = html_candidate_blocks(soup, source.get("selectors"))

        for block in blocks[:source.get("max_blocks", 100)]:
            title, link = best_link_from_block(block, source["base_url"], source.get("link_selector", ""))
            if not title or len(title) < 18:
                rejected_count += 1
                rejection_reasons["invalid"] = rejection_reasons.get("invalid", 0) + 1
                continue

            if link in seen or not is_valid_url(link) or not is_allowed_source_url(link):
                rejected_count += 1
                reason = "duplicate" if link in seen else "external"
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                continue

            candidate_count += 1
            context = clean_html(block.get_text(" ")) if block else title
            if not has_primary_ai_signal(title, context):
                rejected_count += 1
                rejection_reasons["no_ai"] = rejection_reasons.get("no_ai", 0) + 1
                continue

            date = parse_date_text(context, title)
            should_fetch_detail = source.get("fetch_details") and detail_pages_loaded < source.get("max_detail_pages", 10)
            if should_fetch_detail and (
                is_unknown_date(date)
                or not has_explicit_ai_signal(title, context)
                or not passes_article_filter(title, context)
            ):
                detail_text, detail_date = fetch_detail_context(link)
                detail_pages_loaded += 1
                if detail_text:
                    context = clean_html(f"{context} {detail_text}")
                if not is_unknown_date(detail_date):
                    date = detail_date

            relevance_text = context
            if not has_explicit_ai_signal(title, relevance_text):
                rejected_count += 1
                rejection_reasons["no_ai"] = rejection_reasons.get("no_ai", 0) + 1
                continue
            if not passes_article_filter(title, relevance_text):
                rejected_count += 1
                rejection_reasons["no_legal"] = rejection_reasons.get("no_legal", 0) + 1
                continue

            if is_unknown_date(date):
                rejected_count += 1
                rejection_reasons["unknown_date"] = rejection_reasons.get("unknown_date", 0) + 1
                continue
            if not is_recent_date(date):
                rejected_count += 1
                rejection_reasons["old_date"] = rejection_reasons.get("old_date", 0) + 1
                continue

            seen.add(link)
            article = {
                "id": re.sub(r'\W+', '_', link)[:80],
                "title": title[:220],
                "summary": context[:500] if context else source["description"],
                "link": link,
                "date": date,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "source": source["name"],
                "country": source["country"],
                "flag": source["flag"],
                "category": source["category"],
                "source_description": source["description"],
                "relevance_score": relevance_score(title, relevance_text),
                "legal_update_type": legal_update_type(title, relevance_text),
                "legal_priority": legal_priority(title, relevance_text),
            }
            articles.append(attach_analysis(article, title, relevance_text))
    except Exception as e:
        had_error = True
        if isinstance(e, requests.exceptions.HTTPError) and getattr(e.response, "status_code", None) in (403, 404, 410):
            status_code = e.response.status_code
            logger.warning(f"[{source['name']}] HTML-страница недоступна (HTTP {status_code}): {source['url']}")
            update_source_diagnostic(source["name"], status="unavailable", message=f"HTTP {status_code}: page is unavailable or blocks server requests")
        elif isinstance(e, (requests.Timeout, requests.exceptions.ReadTimeout)):
            logger.warning(f"[{source['name']}] Страница не ответила за отведенное время, пропуск: {source['url']}")
            update_source_diagnostic(source["name"], status="timeout", message="request timeout")
        else:
            logger.error(f"[{source['name']}] Ошибка HTML-поиска: {e}")
            update_source_diagnostic(source["name"], status="error", message=str(e)[:220])

    articles = articles[:30]
    if had_error:
        return articles
    mode_summary = summarize_article_modes(articles)
    msg = "ok"
    if not articles:
        msg = format_rejection_summary(candidate_count, 0, rejection_reasons)
        if used_browser_fallback:
            msg += "; browser fallback used"
    elif used_browser_fallback:
        msg = "ok; browser fallback used"
    update_source_diagnostic(
        source["name"],
        status=final_source_status(source["name"]),
        raw_count=candidate_count,
        candidate_count=candidate_count,
        rejected_count=rejected_count,
        message=msg,
        **mode_summary,
    )
    return articles



def configured_official_fallback(source: dict, diagnostic_status: str, message: str) -> list[dict]:
    if not source.get("use_config_fallback") or not source.get("date"):
        return []
    date = parse_date_text(source["date"])
    if not is_recent_date(date) or date_temporal_status(date) == "future":
        return []
    title = source.get("title") or source["name"]
    summary = source.get("description", "")
    if not passes_article_filter(title, summary) and not is_official_ai_law_page(title, summary):
        return []
    article = {
        "id": re.sub(r"\W+", "_", source["url"])[:80],
        "title": title,
        "summary": summary,
        "link": source["url"],
        "date": date,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": source["name"],
        "country": source["country"],
        "flag": source["flag"],
        "category": source["category"],
        "source_description": summary,
        "relevance_score": relevance_score(title, summary),
        "legal_update_type": legal_update_type(title, summary),
        "legal_priority": legal_priority(title, summary),
        "retrieval_status": "configured_fallback",
    }
    result = [attach_analysis(article, title, summary)]
    update_source_diagnostic(
        source["name"],
        status=diagnostic_status,
        raw_count=0,
        candidate_count=1,
        message=f"{message}; material retained from verified configuration",
        **summarize_article_modes(result),
    )
    return result

def scrape_official_page(source: dict) -> list[dict]:
    """Добавляет важную официальную страницу как отдельный материал, если она свежая и релевантная."""
    update_source_diagnostic(source["name"], status="running", url=source.get("url", ""))
    try:
        logger.info(f"[{source['name']}] Проверка официальной страницы: {source['url']}")
        html, used_browser_fallback = fetch_html_text(
            source["url"],
            allow_browser=bool(source.get("browser_fallback", False)),
        )
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.select_one("h1") or soup.select_one("title")
        title = source.get("title") or (clean_html(title_tag.get_text(" ")) if title_tag else source["name"])
        content_nodes = soup.select(
            "main, article, .content, .field, .field__item, p, li, time, "
            ".date, .field--name-created, .gc-byline"
        )
        page_text = clean_html(" ".join(node.get_text(" ") for node in content_nodes))
        if len(page_text) < 240:
            page_text = clean_html(soup.get_text(" "))
        relevance_text = f"{page_text} {source['description']}"
        date = parse_date_text(page_text, title, source["url"], source["description"])
        if source.get("date"):
            fallback_date = parse_date_text(source["date"])
            if not is_unknown_date(fallback_date):
                date = fallback_date

        if not has_primary_ai_signal(title, page_text):
            update_source_diagnostic(source["name"], status="ok", raw_count=1, candidate_count=1, rejected_count=1, message="no primary AI signal")
            return []
        if not has_explicit_ai_signal(title, relevance_text):
            update_source_diagnostic(source["name"], status="ok", raw_count=1, candidate_count=1, rejected_count=1, message="no explicit AI signal")
            return []
        if not passes_article_filter(title, relevance_text) and not is_official_ai_law_page(title, relevance_text):
            update_source_diagnostic(source["name"], status="ok", raw_count=1, candidate_count=1, rejected_count=1, message="official page did not pass legal AI filter")
            return []
        if is_unknown_date(date):
            update_source_diagnostic(source["name"], status="ok", raw_count=1, candidate_count=1, rejected_count=1, message="date is unknown")
            return []
        if date_temporal_status(date) == "future" and not source.get("allow_future_date", False):
            update_source_diagnostic(source["name"], status="ok", raw_count=1, candidate_count=1, rejected_count=1, message="page date is in the future")
            return []
        if not is_recent_date(date):
            update_source_diagnostic(source["name"], status="ok", raw_count=1, candidate_count=1, rejected_count=1, message="date is older than monitoring period")
            return []

        article = {
            "id": re.sub(r'\W+', '_', source["url"])[:80],
            "title": title[:220],
            "summary": (page_text or source["description"])[:500],
            "link": source["url"],
            "date": date,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source": source["name"],
            "country": source["country"],
            "flag": source["flag"],
            "category": source["category"],
            "source_description": source["description"],
            "relevance_score": relevance_score(title, relevance_text),
            "legal_update_type": legal_update_type(title, relevance_text),
            "legal_priority": legal_priority(title, relevance_text),
        }
        result = [attach_analysis(article, title, relevance_text)]
        update_source_diagnostic(
            source["name"],
            status="ok",
            raw_count=1,
            candidate_count=1,
            rejected_count=0,
            message="ok; browser fallback used" if used_browser_fallback else "ok",
            **summarize_article_modes(result),
        )
        return result
    except Exception as e:
        if isinstance(e, requests.exceptions.HTTPError) and getattr(e.response, "status_code", None) in (403, 404, 410):
            status_code = e.response.status_code
            logger.warning(f"[{source['name']}] Официальная страница недоступна (HTTP {status_code}): {source['url']}")
            update_source_diagnostic(source["name"], status="unavailable", message=f"HTTP {status_code}: page is unavailable or blocks server requests")
        elif isinstance(e, (requests.Timeout, requests.exceptions.ReadTimeout)):
            logger.warning(f"[{source['name']}] Страница не ответила за отведенное время, пропуск: {source['url']}")
            update_source_diagnostic(source["name"], status="timeout", message="request timeout")
        else:
            logger.error(f"[{source['name']}] Ошибка страницы: {e}")
            update_source_diagnostic(source["name"], status="error", message=str(e)[:220])
        current = SOURCE_DIAGNOSTICS.get(source["name"], {})
        return configured_official_fallback(
            source,
            current.get("status", "error"),
            current.get("message", str(e)[:220]),
        )


# ─────────────────────────────────────────────
# ГЛАВНАЯ ФУНКЦИЯ СБОРА ДАННЫХ
# ─────────────────────────────────────────────

def fetch_all_articles(include_diagnostics: bool = False):
    """Собирает статьи из всех источников параллельно."""
    reset_source_diagnostics()
    for source in ALL_SOURCES:
        update_source_diagnostic(
            source["name"],
            status="pending",
            url=source.get("url", ""),
            country=source.get("country", ""),
            message="waiting",
        )
    all_articles = []

    # Параллельный сбор: RSS-ленты + скраперы одновременно
    tasks = [(fetch_rss, src) for src in RSS_SOURCES]
    tasks += [(lambda: scrape_eurlex_ai(), None),
              (lambda: scrape_congress_ai(), None)]

    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        rss_futures = {pool.submit(fetch_rss, src): src["name"] for src in RSS_SOURCES}
        scraper_futures = {
            pool.submit(scrape_eurlex_ai): "EUR-Lex (прямой поиск)",
            pool.submit(scrape_congress_ai): "US Congress (прямой поиск)",
            pool.submit(scrape_federal_register_ai): "Federal Register (официальный API)",
        }
        scraper_futures.update({
            pool.submit(scrape_html_search_source, src): src["name"]
            for src in HTML_SEARCH_SOURCES
        })
        scraper_futures.update({
            pool.submit(scrape_official_page, src): src["name"]
            for src in OFFICIAL_PAGE_SOURCES
        })
        all_futures = {**rss_futures, **scraper_futures}

        try:
            completed = as_completed(all_futures, timeout=COLLECT_TIMEOUT)
            for future in completed:
                name = all_futures[future]
                try:
                    result = future.result(timeout=1)
                    all_articles.extend(result)
                    current_diag = SOURCE_DIAGNOSTICS.get(name, {})
                    current_message = current_diag.get("message", "")
                    if final_source_status(name) == "ok" and current_message in {"", "waiting", "running"}:
                        result_count = len(result)
                        update_source_diagnostic(
                            name,
                            status="ok",
                            raw_count=max(int(current_diag.get("raw_count", 0)), result_count),
                            candidate_count=max(int(current_diag.get("candidate_count", 0)), result_count),
                            message="ok" if result else "прочитано 0, релевантных материалов не найдено",
                            **summarize_article_modes(result),
                        )
                    logger.info(f"[{name}] Релевантных: {len(result)}")
                except Exception as e:
                    logger.error(f"[{name}] Ошибка в потоке: {e}")
                    update_source_diagnostic(name, status="error", message=str(e)[:220])
        except concurrent.futures.TimeoutError:
            unfinished = [name for future, name in all_futures.items() if not future.done()]
            for future in all_futures:
                if not future.done():
                    future.cancel()
                    update_source_diagnostic(all_futures[future], status="timeout", message=f"global timeout {COLLECT_TIMEOUT}s")
            logger.warning(
                "Сбор остановлен по общему таймауту %s сек. Не дождались источников: %s",
                COLLECT_TIMEOUT,
                ", ".join(unfinished[:20]) or "нет",
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Межсайтовая дедупликация: URL, идентификаторы актов, похожие заголовки,
    # страна, тип правового события и близость дат.
    unique = deduplicate_articles(all_articles)

    # Сортировка по реальной дате материала: сначала самые новые и актуальные.
    unique.sort(
        key=lambda article: (
            to_datetime(article.get("date", "")),
            int(article.get("legal_priority", 1)),
            int(article.get("relevance_score", 0)),
        ),
        reverse=True,
    )

    logger.info(f"Итого уникальных релевантных статей: {len(unique)}")
    if include_diagnostics:
        return {
            "articles": unique,
            "diagnostics": source_diagnostics_snapshot(),
        }
    return unique


def get_stats(articles: list[dict]) -> dict:
    """Возвращает статистику по статьям."""
    countries = {}
    categories = {}
    sources = {}
    strict_count = 0
    monitoring_count = 0

    for a in articles:
        countries[a["country"]] = countries.get(a["country"], 0) + 1
        categories[a["category"]] = categories.get(a["category"], 0) + 1
        sources[a["source"]] = sources.get(a["source"], 0) + 1
        if a.get("strict_match"):
            strict_count += 1
        else:
            monitoring_count += 1

    return {
        "total": len(articles),
        "strict_total": strict_count,
        "monitoring_total": monitoring_count,
        "countries": dict(sorted(countries.items(), key=lambda x: -x[1])),
        "categories": dict(sorted(categories.items(), key=lambda x: -x[1])),
        "top_sources": dict(
            list(sorted(sources.items(), key=lambda x: -x[1]))[:5]
        ),
    }
