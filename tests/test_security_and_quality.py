import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

import app
import scraper


def password_hash(password: str) -> str:
    salt = "test-salt"
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        10_000,
    ).hex()
    return f"pbkdf2_sha256$10000${salt}${digest}"


class AppSecurityTests(unittest.TestCase):
    def setUp(self):
        self.original_users = dict(app.USERS)
        self.original_dummy = app.DUMMY_PASSWORD_HASH
        app.USERS.clear()
        app.USERS["tester"] = password_hash("Secure1!")
        app.DUMMY_PASSWORD_HASH = password_hash("dummy-password")
        app.LOGIN_ATTEMPTS.clear()
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()

    def tearDown(self):
        app.USERS.clear()
        app.USERS.update(self.original_users)
        app.DUMMY_PASSWORD_HASH = self.original_dummy
        app.LOGIN_ATTEMPTS.clear()

    def csrf(self):
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "test-csrf"
        return "test-csrf"

    def authenticate(self):
        token = self.csrf()
        response = self.client.post(
            "/login",
            data={
                "username": "tester",
                "password": "Secure1!",
                "_csrf_token": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def test_login_rejects_missing_csrf(self):
        response = self.client.post(
            "/login",
            data={"username": "tester", "password": "Secure1!"},
        )
        self.assertEqual(response.status_code, 400)

    def test_open_redirect_is_blocked(self):
        token = self.csrf()
        response = self.client.post(
            "/login?next=https://evil.example/phishing",
            data={
                "username": "tester",
                "password": "Secure1!",
                "_csrf_token": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_login_rate_limit(self):
        token = self.csrf()
        for _ in range(app.LOGIN_RATE_MAX_ATTEMPTS):
            response = self.client.post(
                "/login",
                data={
                    "username": "tester",
                    "password": "wrong",
                    "_csrf_token": token,
                },
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/login",
            data={
                "username": "tester",
                "password": "wrong",
                "_csrf_token": token,
            },
        )
        self.assertEqual(response.status_code, 429)

    def test_refresh_requires_csrf(self):
        self.authenticate()
        response = self.client.post("/api/refresh")
        self.assertEqual(response.status_code, 403)

    def test_refresh_accepts_valid_csrf(self):
        token = self.authenticate()
        with patch.object(app, "acquire_refresh_lock", return_value=True), patch.object(
            app, "background_refresh"
        ):
            response = self.client.post(
                "/api/refresh",
                headers={"X-CSRF-Token": token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "started")

    def test_api_pagination_validation(self):
        self.authenticate()
        self.assertEqual(self.client.get("/api/articles?page=abc").status_code, 400)
        self.assertEqual(self.client.get("/api/articles?per_page=0").status_code, 400)
        self.assertEqual(self.client.get("/api/articles?per_page=-1").status_code, 400)
        self.assertEqual(self.client.get("/api/articles?per_page=51").status_code, 400)

    def test_invalid_mode_is_rejected(self):
        self.authenticate()
        self.assertEqual(self.client.get("/api/articles?mode=invalid").status_code, 400)
        self.assertEqual(self.client.get("/api/stats?mode=invalid").status_code, 400)

    def test_security_headers_and_no_cors(self):
        response = self.client.get("/login", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_default_secret_is_not_used(self):
        self.assertNotEqual(
            app.app.secret_key,
            "change-this-secret-key-before-production",
        )


    def test_internal_app_is_not_indexed(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Disallow: /", response.data)
        login = self.client.get("/login")
        self.assertIn(b'content="noindex,nofollow,noarchive"', login.data)

    def test_diagnostic_transport_error_is_sanitized(self):
        message = (
            "HTTPSConnectionPool(host='example.test', port=443): "
            "Max retries exceeded with url: /feed"
        )
        sanitized = app.sanitize_diagnostic_message(message)
        self.assertNotIn("HTTPSConnectionPool", sanitized)
        self.assertIn("соединение", sanitized.lower())


class DataQualityTests(unittest.TestCase):
    def test_malicious_url_is_rejected(self):
        malicious = 'https://www.nist.gov/" onmouseover="alert(1)'
        self.assertFalse(scraper.is_valid_url(malicious))
        self.assertFalse(scraper.is_allowed_source_url(malicious))

    def test_excel_formula_is_neutralized(self):
        article = {
            "date": "2026-06-20",
            "country": "EU",
            "category": "Законодательство",
            "source": "Test",
            "title": '=HYPERLINK("https://evil.example","Open")',
            "summary": "+SUM(1,1)",
            "link": "https://www.nist.gov/test",
            "strict_match": True,
            "nlp_label": "strict",
            "nlp_confidence": 99,
            "nlp_reason": "test",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.xlsx"
            app.build_excel_report([article], [], "2026-06-20T00:00:00+00:00", path)
            workbook = load_workbook(path, data_only=False)
            sheet = workbook.worksheets[0]
            self.assertEqual(sheet["H2"].data_type, "s")
            self.assertTrue(sheet["H2"].value.startswith("'="))
            self.assertTrue(sheet["I2"].value.startswith("'+"))

    def test_cache_reclassifies_and_drops_irrelevant_article(self):
        valid = {
            "title": "Artificial Intelligence Act final rule adopted",
            "summary": "The regulator adopted binding requirements for AI systems.",
            "link": "https://www.nist.gov/test-ai-law",
            "date": "2026-06-01T00:00:00+00:00",
            "source": "NIST News",
            "country": "USA",
            "category": "Стандарты",
            "nlp_label": "strict",
            "filter_mode": "strict",
        }
        irrelevant = {
            "title": "Advisory Committee on Medical Uses of Isotopes",
            "summary": "Routine meeting notice.",
            "link": "https://www.federalregister.gov/documents/test",
            "date": "2026-06-01T00:00:00+00:00",
            "source": "Federal Register (официальный API)",
            "country": "USA",
            "category": "Регулятор",
            "nlp_label": "strict",
            "filter_mode": "strict",
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "cache_version": app.CACHE_VERSION,
                        "sources_count": len(app.ALL_SOURCES),
                        "articles": [valid, irrelevant],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_cache = app.CACHE_FILE
            old_mtime = app._cache_file_mtime
            try:
                app.CACHE_FILE = cache
                self.assertTrue(app.load_cache_from_disk())
                links = {item["link"] for item in app._cache["articles"]}
                self.assertIn(valid["link"], links)
                self.assertNotIn(irrelevant["link"], links)
            finally:
                app.CACHE_FILE = old_cache
                app._cache_file_mtime = old_mtime

    def test_refresh_failure_is_exposed(self):
        with patch.object(app, "fetch_all_articles", side_effect=RuntimeError("source failure")), patch.object(
            app, "release_refresh_lock"
        ):
            app.refresh_articles(lock_acquired=True)
        self.assertEqual(app._cache["refresh_status"], "failed")
        self.assertIn("source failure", app._cache["refresh_error"])

    def test_history_updates_do_not_lose_records(self):
        with tempfile.TemporaryDirectory() as directory:
            old_dir = app.HISTORY_DIR
            old_index = app.HISTORY_INDEX_FILE
            old_builder = app.build_excel_report
            try:
                app.HISTORY_DIR = Path(directory)
                app.HISTORY_INDEX_FILE = Path(directory) / "history.json"
                app.build_excel_report = lambda articles, diagnostics, updated_at, path: path.touch()
                barrier = threading.Barrier(2)

                def worker():
                    barrier.wait()
                    app.save_update_history([], [])

                threads = [threading.Thread(target=worker) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(len(app.read_history_index()), 2)
            finally:
                app.HISTORY_DIR = old_dir
                app.HISTORY_INDEX_FILE = old_index
                app.build_excel_report = old_builder


if __name__ == "__main__":
    unittest.main()
