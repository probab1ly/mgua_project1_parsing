import csv
import json
from datetime import datetime
from pathlib import Path


CACHE_FILE = Path("cache.json")
MD_FILE = Path("PARSED_LINKS.md")
CSV_FILE = Path("PARSED_LINKS.csv")


def clean(value):
    return str(value or "").replace("\n", " ").strip()


def main():
    if not CACHE_FILE.exists():
        MD_FILE.write_text(
            "# Ссылки после парсинга\n\n"
            "Кэш `cache.json` не найден. Сначала нажмите «Обновить данные» в приложении.\n",
            encoding="utf-8",
        )
        return

    data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    articles = data.get("articles", [])

    md_lines = [
        "# Ссылки после парсинга AI Regulation Monitor",
        "",
        f"Сформировано: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Последнее обновление кэша: {clean(data.get('last_updated')) or 'не указано'}",
        f"Всего ссылок: {len(articles)}",
        "",
        "> После нового парсинга запустите `python export_links.py`, чтобы пересоздать этот файл.",
        "",
    ]

    rows = []
    current_region = None
    for index, article in enumerate(articles, 1):
        region = clean(article.get("country")) or "Unknown"
        if region != current_region:
            current_region = region
            md_lines.extend([f"## {region}", ""])

        row = {
            "№": index,
            "Дата": clean(article.get("date"))[:10],
            "Регион": region,
            "Тип": clean(article.get("legal_update_type")) or "Правовое событие",
            "Источник": clean(article.get("source")),
            "Заголовок": clean(article.get("title")),
            "URL": clean(article.get("link")),
        }
        rows.append(row)

        md_lines.extend([
            f"{index}. **{row['Заголовок']}**",
            f"   - Дата: {row['Дата'] or 'не указана'}",
            f"   - Регион: {row['Регион']}",
            f"   - Тип: {row['Тип']}",
            f"   - Источник: {row['Источник']}",
            f"   - URL: {row['URL']}",
            "",
        ])

    MD_FILE.write_text("\n".join(md_lines), encoding="utf-8")

    with CSV_FILE.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["№", "Дата", "Регион", "Тип", "Источник", "Заголовок", "URL"])
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
