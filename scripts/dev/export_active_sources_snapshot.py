from collections import Counter
from pathlib import Path
import json
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
import scraper

OUT = PROJECT_ROOT / "docs" / "_work" / "active_sources_snapshot.json"

snapshot = []
for source in scraper.ALL_SOURCES:
    snapshot.append({
        "name": source.get("name", ""),
        "url": source.get("url", ""),
        "country": source.get("country", ""),
        "category": source.get("category", ""),
        "description": source.get("description", ""),
        "source_type": source.get("source_type", ""),
        "language": source.get("language", ""),
        "format": source.get("format", ""),
    })
OUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
print(OUT)
print("sources_count", len(snapshot))
print("countries", dict(Counter(item["country"] for item in snapshot)))
