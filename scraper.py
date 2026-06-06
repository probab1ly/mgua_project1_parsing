import feedparser
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib.parse


# Таймаут для каждого HTTP-запроса (секунды)
REQUEST_TIMEOUT = 12
SLOW_REQUEST_TIMEOUT = 24
HTML_REQUEST_TIMEOUT = 10
# Максимум параллельных потоков
MAX_WORKERS = 8
# Общий лимит одного ручного обновления, чтобы приложение не зависало на медленных сайтах.
COLLECT_TIMEOUT = 180
# Берем актуальные материалы минимум за последний год.
RECENT_DAYS = 365

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

def fix_url(url: str) -> str:
    """Убирает лишние точки и пробелы из URL."""
    if not url:
        return url
    url = url.strip()

    url = re.sub(r'(https?://[^/]+)\./([^\s])', r'\1/\2', url)  # с путём
    url = re.sub(r'(https?://[^/]+)\.$', r'\1', url)             # без пути
    return url

def is_valid_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        return all([parsed.scheme in ("http", "https"), parsed.netloc])
    except Exception:
        return False
    
# ─────────────────────────────────────────────
# ИСТОЧНИКИ RSS / ATOM
# ─────────────────────────────────────────────
RSS_SOURCES = [
    # ── ЕВРОПЕЙСКИЙ СОЮЗ ──────────────────────
    {
        "name": "EUR-Lex (EU Law)",
        "url": "https://eur-lex.europa.eu/rss/rss.xml?type=qd&qid=1&lg=EN",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Законодательство",
        "description": "Официальный правовой портал ЕС",
    },
    {
        "name": "European Parliament News",
        "url": "https://www.europarl.europa.eu/rss/doc/news-and-press-releases/en.rss",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Парламент",
        "description": "Новости Европейского парламента",
    },
    {
        "name": "Council of the EU Press Releases",
        "url": "https://www.consilium.europa.eu/en/rss/press-releases.xml",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Совет ЕС",
        "description": "Пресс-релизы Совета ЕС и Европейского совета",
    },
    {
        "name": "EDPB News",
        "url": "https://www.edpb.europa.eu/rss/news_en",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Регулятор",
        "description": "Новости Европейского совета по защите данных",
    },
    {
        "name": "EDPB Publications",
        "url": "https://www.edpb.europa.eu/rss/publications_en",
        "country": "EU",
        "flag": "🇪🇺",
        "category": "Руководства",
        "description": "Публикации, мнения и руководства EDPB",
    },
    # ── США ───────────────────────────────────
    {
        "name": "White House Briefings",
        "url": "https://www.whitehouse.gov/feed/",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Исполнительная власть",
        "description": "Официальный сайт Белого дома",
    },
    {
        "name": "FTC News",
        "url": "https://www.ftc.gov/feeds/news.xml",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Регулятор",
        "description": "Федеральная торговая комиссия США",
    },
    {
        "name": "NIST News",
        "url": "https://www.nist.gov/news-events/news/rss.xml",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Стандарты",
        "description": "Национальный институт стандартов и технологий",
    },
    {
        "name": "Federal Register – AI",
        "url": "https://www.federalregister.gov/documents/search.rss?conditions%5Bterm%5D=artificial+intelligence",
        "country": "USA",
        "flag": "🇺🇸",
        "category": "Регулятор",
        "description": "Официальный журнал федеральных правил и уведомлений США",
    },
    # ── МЕЖДУНАРОДНЫЕ ОРГАНИЗАЦИИ ─────────────
    {
        "name": "OECD AI Policy Observatory",
        "url": "https://oecd.ai/rss.xml",
        "country": "OECD",
        "flag": "🌐",
        "category": "Международные",
        "description": "ОЭСР — наблюдатель за политикой в области ИИ",
    },
    {
        "name": "UNESCO AI Ethics",
        "url": "https://www.unesco.org/en/rss/artificial-intelligence",
        "country": "UNESCO",
        "flag": "🌐",
        "category": "Международные",
        "description": "ЮНЕСКО — этика искусственного интеллекта",
    },
    # ── ВЕЛИКОБРИТАНИЯ ────────────────────────
    {
        "name": "UK GOV – Technology Policy",
        "url": "https://www.gov.uk/search/news-and-communications.atom?keywords=artificial+intelligence&organisations[]=department-for-science-innovation-and-technology",
        "country": "UK",
        "flag": "🇬🇧",
        "category": "Законодательство",
        "description": "Правительство Великобритании — технологическая политика",
    },
    # ── СПЕЦИАЛИЗИРОВАННЫЕ ПРАВОВЫЕ И POLICY-ИСТОЧНИКИ ────────────────
    {
        "name": "AI Policy Newsletter (CSET)",
        "url": "https://cset.georgetown.edu/feed/",
        "country": "Global",
        "flag": "🌐",
        "category": "Аналитика",
        "description": "Центр безопасности и новых технологий Джорджтауна",
    },
    {
        "name": "IAPP AI Governance",
        "url": "https://iapp.org/feed/",
        "country": "Global",
        "flag": "🌐",
        "category": "Правовая аналитика",
        "description": "Новости IAPP по privacy, AI governance и регулированию",
    },
    {
        "name": "Japan Digital Agency",
        "url": "https://www.digital.go.jp/rss/news.xml",
        "country": "Japan",
        "flag": "🇯🇵",
        "category": "Госуправление",
        "description": "Новости Digital Agency Japan, включая ИИ в государственном секторе",
    },
    {
        "name": "Japan METI",
        "url": "https://www.meti.go.jp/ml_index_en_atom.xml",
        "country": "Japan",
        "flag": "🇯🇵",
        "category": "Регулятор",
        "description": "Новости Министерства экономики, торговли и промышленности Японии",
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


def build_targeted_sources() -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).date().isoformat()
    sources = []
    for query in TARGETED_FEED_QUERIES:
        encoded = urllib.parse.quote_plus(query)
        label = query.title()
        sources.extend([
            {
                "name": f"Federal Register – {label}",
                "url": (
                    "https://www.federalregister.gov/documents/search.rss"
                    f"?conditions%5Bterm%5D={encoded}"
                    f"&conditions%5Bpublication_date%5D%5Bgte%5D={cutoff}"
                ),
                "country": "USA",
                "flag": "🇺🇸",
                "category": "Регулятор",
                "description": "Официальный журнал федеральных правил и уведомлений США",
            },
            {
                "name": f"UK GOV – {label}",
                "url": (
                    "https://www.gov.uk/search/news-and-communications.atom"
                    f"?keywords={encoded}"
                ),
                "country": "UK",
                "flag": "🇬🇧",
                "category": "Законодательство",
                "description": "Правительство Великобритании — новости и документы по регулированию ИИ",
            },
        ])
    return sources


RSS_SOURCES.extend(build_targeted_sources())

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
    "www.meti.go.jp",
    "www.korea.net",
    "www.msit.go.kr",
    "www12.senado.leg.br",
    "www.loc.gov",
}


def hostname(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def is_allowed_source_url(url: str) -> bool:
    host = hostname(url)
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_SOURCE_DOMAINS)


def request_timeout_for(url: str) -> int:
    slow_hosts = {
        "www.canada.ca",
        "www.industry.gov.au",
        "www.ag.gov.au",
        "www.oaic.gov.au",
        "www12.senado.leg.br",
    }
    return SLOW_REQUEST_TIMEOUT if hostname(url) in slow_hosts else REQUEST_TIMEOUT


def html_timeout_for(url: str) -> int:
    return min(request_timeout_for(url), HTML_REQUEST_TIMEOUT)


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

AI_SUBJECT_TERMS_LOWER = [term.lower() for term in AI_SUBJECT_TERMS]
LEGAL_RELEVANCE_TERMS_LOWER = [term.lower() for term in LEGAL_RELEVANCE_TERMS]
STRONG_AI_REGULATION_TERMS_LOWER = [term.lower() for term in STRONG_AI_REGULATION_TERMS]
ADOPTION_AND_LEGAL_EVENT_TERMS_LOWER = [term.lower() for term in ADOPTION_AND_LEGAL_EVENT_TERMS]
HIGH_VALUE_LEGAL_UPDATE_TERMS_LOWER = [term.lower() for term in HIGH_VALUE_LEGAL_UPDATE_TERMS]
LEGAL_INSTRUMENT_TERMS_LOWER = [term.lower() for term in LEGAL_INSTRUMENT_TERMS]
LEGAL_ACTION_TERMS_LOWER = [term.lower() for term in LEGAL_ACTION_TERMS]


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
        "relevance_score": relevance_score(title, full_text),
        "legal_update_type": legal_update_type(title, full_text),
        "legal_priority": legal_priority(title, full_text),
        "analysis_confidence": analysis["confidence"],
        "analysis_reason": analysis["reason"],
        "matched_ai_terms": analysis["ai_terms"],
        "matched_legal_instruments": analysis["legal_instruments"],
        "matched_legal_actions": analysis["legal_actions"],
    }


def attach_analysis(article: dict, title: str, text: str) -> dict:
    analysis = analyze_legal_ai_relevance(title, text)
    article["analysis_confidence"] = analysis["confidence"]
    article["analysis_reason"] = analysis["reason"]
    article["matched_ai_terms"] = analysis["ai_terms"]
    article["matched_legal_instruments"] = analysis["legal_instruments"]
    article["matched_legal_actions"] = analysis["legal_actions"]
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
                dt = dateparser.parse(match.group(0), dayfirst=True)
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
    return dt >= cutoff


def clean_html(text: str) -> str:
    """Удаляет HTML-теги из текста."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return re.sub(r'\s+', ' ', soup.get_text()).strip() # поиск и замена текста 


def fetch_rss(source: dict) -> list[dict]:
    """Загружает и фильтрует записи из RSS/Atom-ленты."""
    articles = []
    try:
        if not is_allowed_source_url(source["url"]):
            logger.warning(f"[{source['name']}] Источник не входит в белый список доменов, пропуск")
            return []

        feed = feedparser.parse(source["url"], agent=HEADERS["User-Agent"], request_headers=HEADERS)
        logger.info(f"[{source['name']}] Получено записей: {len(feed.entries)}")

        if feed.get("status", 200) in (403, 404, 410):
            logger.warning(f"[{source['name']}] Недоступен (HTTP {feed.get('status')}), пропуск")
            return []
        if feed.bozo and not feed.entries:
            logger.warning(f"[{source['name']}] Пустая или битая лента, пропуск")
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
                continue
            if not is_allowed_source_url(link):
                continue
            if is_broad_source(source) and not has_primary_ai_signal(title, summary):
                continue
            if not has_explicit_ai_signal(title, summary):
                continue
            if not is_ai_relevant(title, summary):
                continue
            date = parse_date(entry)
            if not is_recent_date(date):
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
        if isinstance(e, (requests.Timeout, requests.exceptions.ReadTimeout)):
            logger.warning(f"[{source['name']}] Источник не ответил за отведенное время, пропуск: {source['url']}")
        else:
            logger.error(f"[{source['name']}] Ошибка: {e}")

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
            soup = BeautifulSoup(resp.text, "html.parser")

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
                if not is_ai_relevant(title, relevance_text) and not is_official_ai_law_page(title, relevance_text):
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
            soup = BeautifulSoup(resp.text, "html.parser")

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
                if not is_ai_relevant(title, relevance_text) and not is_official_ai_law_page(title, relevance_text):
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
                if not has_primary_ai_signal(title, summary):
                    continue
                if not has_explicit_ai_signal(title, relevance_text):
                    continue
                if not is_ai_relevant(title, relevance_text):
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
        "name": "ICO – AI Search",
        "url": "https://ico.org.uk/search/?q=artificial%20intelligence",
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
        "name": "Canada.ca – AI",
        "url": "https://www.canada.ca/en/services/science/innovation/artificial-intelligence.html",
        "country": "Canada",
        "flag": "🇨🇦",
        "category": "Госуправление",
        "description": "Официальная страница Канады по AI policy, regulation и responsible AI",
        "base_url": "https://www.canada.ca",
    },
    {
        "name": "Canada.ca – Responsible AI",
        "url": "https://www.canada.ca/en/government/system/digital-government/digital-government-innovations/responsible-use-ai.html",
        "country": "Canada",
        "flag": "🇨🇦",
        "category": "Руководства",
        "description": "Responsible use of AI in Government of Canada",
        "base_url": "https://www.canada.ca",
    },
    {
        "name": "Australia Industry – AI",
        "url": "https://www.industry.gov.au/search?query=artificial%20intelligence%20regulation",
        "country": "Australia",
        "flag": "🇦🇺",
        "category": "Госуправление",
        "description": "Department of Industry, Science and Resources: AI policy and regulation",
        "base_url": "https://www.industry.gov.au",
    },
    {
        "name": "Australia Attorney-General – AI",
        "url": "https://www.ag.gov.au/search?search=artificial%20intelligence",
        "country": "Australia",
        "flag": "🇦🇺",
        "category": "Законодательство",
        "description": "Attorney-General's Department: правовые материалы и реформы, связанные с ИИ",
        "base_url": "https://www.ag.gov.au",
    },
    {
        "name": "Australia OAIC – AI",
        "url": "https://www.oaic.gov.au/search?query=artificial%20intelligence",
        "country": "Australia",
        "flag": "🇦🇺",
        "category": "Регулятор",
        "description": "Office of the Australian Information Commissioner: privacy, automated decisions and AI",
        "base_url": "https://www.oaic.gov.au",
    },
    {
        "name": "Singapore IMDA – AI",
        "url": "https://www.imda.gov.sg/search?keyword=artificial%20intelligence",
        "country": "Singapore",
        "flag": "🇸🇬",
        "category": "Регулятор",
        "description": "IMDA: AI governance frameworks, press releases and guidance",
        "base_url": "https://www.imda.gov.sg",
    },
    {
        "name": "Singapore PDPC – AI",
        "url": "https://www.pdpc.gov.sg/search?keyword=artificial%20intelligence",
        "country": "Singapore",
        "flag": "🇸🇬",
        "category": "Регулятор",
        "description": "PDPC Singapore: AI governance and data protection resources",
        "base_url": "https://www.pdpc.gov.sg",
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
    {
        "name": "South Korea Korea.net – AI",
        "url": "https://www.korea.net/Search?keyword=artificial%20intelligence%20act",
        "country": "South Korea",
        "flag": "🇰🇷",
        "category": "Законодательство",
        "description": "Official Korea.net policy news, включая AI Basic Act",
        "base_url": "https://www.korea.net",
    },
    {
        "name": "Brazil Senate – AI",
        "url": "https://www12.senado.leg.br/noticias/busca?SearchableText=intelig%C3%AAncia%20artificial",
        "country": "Brazil",
        "flag": "🇧🇷",
        "category": "Законопроекты",
        "description": "Новости Федерального сената Бразилии по законопроектам об ИИ",
        "base_url": "https://www12.senado.leg.br",
    },
    {
        "name": "Library of Congress GLM – AI",
        "url": "https://www.loc.gov/search/?fa=partof:global+legal+monitor&q=artificial+intelligence",
        "country": "Global",
        "flag": "🌐",
        "category": "Правовой мониторинг",
        "description": "Global Legal Monitor: официальные правовые обзоры по странам",
        "base_url": "https://www.loc.gov",
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
    },
    {
        "name": "Singapore AI Governance Framework",
        "url": "https://www.pdpc.gov.sg/help-and-resources/2020/01/model-ai-governance-framework",
        "country": "Singapore",
        "flag": "🇸🇬",
        "category": "Руководства",
        "description": "Singapore approach to AI governance",
    },
    {
        "name": "Japan Government AI GENAI",
        "url": "https://www.digital.go.jp/en/policies/gennai",
        "country": "Japan",
        "flag": "🇯🇵",
        "category": "Госуправление",
        "description": "Government AI GENAI policy and releases",
    },
    {
        "name": "South Korea AI Basic Act",
        "url": "https://www.korea.net/NewsFocus/policies/view?articleId=286183",
        "country": "South Korea",
        "flag": "🇰🇷",
        "category": "Законодательство",
        "description": "Official Korea.net policy news on AI Basic Act",
    },
    {
        "name": "Brazil AI Bill",
        "url": "https://www.loc.gov/item/global-legal-monitor/2025-05-23/brazil-senate-advances-discussions-on-bill-to-regulate-ai-use/",
        "country": "Brazil",
        "flag": "🇧🇷",
        "category": "Законопроекты",
        "description": "Brazil Senate advances discussions on bill to regulate AI use",
    },
]

ALL_SOURCES = RSS_SOURCES + HTML_SEARCH_SOURCES + DIRECT_SEARCH_SOURCES + OFFICIAL_PAGE_SOURCES


def scrape_html_search_source(source: dict) -> list[dict]:
    """Собирает ссылки с профильных правовых страниц без RSS."""
    articles = []
    seen = set()
    try:
        logger.info(f"[{source['name']}] HTML-поиск: {source['url']}")
        resp = SESSION.get(source["url"], timeout=html_timeout_for(source["url"]))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for link_tag in soup.select("a[href]"):
            title = clean_html(link_tag.get_text(" "))
            href = link_tag.get("href", "")
            if not title or len(title) < 18:
                continue

            link = urllib.parse.urljoin(source["base_url"], href)
            link = fix_url(link)
            if link in seen or not is_valid_url(link) or not is_allowed_source_url(link):
                continue

            parent = link_tag.find_parent(["article", "li", "div", "section"]) or link_tag.parent
            context = clean_html(parent.get_text(" ")) if parent else title
            relevance_text = context
            if not has_primary_ai_signal(title, context):
                continue
            if not has_explicit_ai_signal(title, relevance_text):
                continue
            if not is_ai_relevant(title, relevance_text):
                continue

            date = parse_date_text(context, title, link)
            if not is_recent_date(date):
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
        if isinstance(e, (requests.Timeout, requests.exceptions.ReadTimeout)):
            logger.warning(f"[{source['name']}] Страница не ответила за отведенное время, пропуск: {source['url']}")
        else:
            logger.error(f"[{source['name']}] Ошибка HTML-поиска: {e}")

    return articles[:30]


def scrape_official_page(source: dict) -> list[dict]:
    """Добавляет важную официальную страницу как отдельный материал, если она свежая и релевантная."""
    try:
        logger.info(f"[{source['name']}] Проверка официальной страницы: {source['url']}")
        resp = SESSION.get(source["url"], timeout=html_timeout_for(source["url"]))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        title_tag = soup.select_one("h1") or soup.select_one("title")
        title = clean_html(title_tag.get_text(" ")) if title_tag else source["name"]
        page_text = clean_html(" ".join(p.get_text(" ") for p in soup.select("p, time, .date, .field--name-created, .gc-byline")))
        relevance_text = page_text
        date = parse_date_text(page_text, title, source["url"], source["description"])

        if not has_primary_ai_signal(title, page_text):
            return []
        if not has_explicit_ai_signal(title, relevance_text):
            return []
        if not is_ai_relevant(title, relevance_text) and not is_official_ai_law_page(title, relevance_text):
            return []
        if is_unknown_date(date):
            return []
        if not is_recent_date(date):
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
        return [attach_analysis(article, title, relevance_text)]
    except Exception as e:
        if isinstance(e, (requests.Timeout, requests.exceptions.ReadTimeout)):
            logger.warning(f"[{source['name']}] Страница не ответила за отведенное время, пропуск: {source['url']}")
        else:
            logger.error(f"[{source['name']}] Ошибка страницы: {e}")
        return []


# ─────────────────────────────────────────────
# ГЛАВНАЯ ФУНКЦИЯ СБОРА ДАННЫХ
# ─────────────────────────────────────────────

def fetch_all_articles() -> list[dict]:
    """Собирает статьи из всех источников параллельно."""
    all_articles = []

    # Параллельный сбор: RSS-ленты + скраперы одновременно
    tasks = [(fetch_rss, src) for src in RSS_SOURCES]
    tasks += [(lambda: scrape_eurlex_ai(), None),
              (lambda: scrape_congress_ai(), None)]

    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        rss_futures = {pool.submit(fetch_rss, src): src["name"] for src in RSS_SOURCES}
        scraper_futures = {
            pool.submit(scrape_eurlex_ai): "EUR-Lex (прямой)",
            pool.submit(scrape_congress_ai): "Congress.gov",
            pool.submit(scrape_federal_register_ai): "Federal Register API",
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
                    logger.info(f"[{name}] Релевантных: {len(result)}")
                except Exception as e:
                    logger.error(f"[{name}] Ошибка в потоке: {e}")
        except concurrent.futures.TimeoutError:
            unfinished = [name for future, name in all_futures.items() if not future.done()]
            for future in all_futures:
                if not future.done():
                    future.cancel()
            logger.warning(
                "Сбор остановлен по общему таймауту %s сек. Не дождались источников: %s",
                COLLECT_TIMEOUT,
                ", ".join(unfinished[:20]) or "нет",
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Дедубликация по ссылке
    seen_links = set()
    unique = []
    for a in all_articles:
        if a["link"] not in seen_links:
            seen_links.add(a["link"])
            unique.append(a)

    # Сортировка по реальной дате материала: сначала самые новые и актуальные.
    unique.sort(
        key=lambda article: (
            int(article.get("legal_priority", 1)),
            to_datetime(article.get("date", "")),
            int(article.get("relevance_score", 0)),
        ),
        reverse=True,
    )

    logger.info(f"Итого уникальных релевантных статей: {len(unique)}")
    return unique


def get_stats(articles: list[dict]) -> dict:
    """Возвращает статистику по статьям."""
    countries = {}
    categories = {}
    sources = {}

    for a in articles:
        countries[a["country"]] = countries.get(a["country"], 0) + 1
        categories[a["category"]] = categories.get(a["category"], 0) + 1
        sources[a["source"]] = sources.get(a["source"], 0) + 1

    return {
        "total": len(articles),
        "countries": dict(sorted(countries.items(), key=lambda x: -x[1])),
        "categories": dict(sorted(categories.items(), key=lambda x: -x[1])),
        "top_sources": dict(
            list(sorted(sources.items(), key=lambda x: -x[1]))[:5]
        ),
    }
