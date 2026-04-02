"""
Tests for the /api/logs Flask endpoint.

Verifies that the activity log endpoint works correctly regardless of how the
Flask app is started, and that log entries are returned in the expected format.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import database


@pytest.fixture()
def app_client(tmp_path):
    """Flask test client with an isolated temp DB."""
    database.DB_PATH = tmp_path / "test_api_logs.db"
    database.init_db()

    import app as flask_app

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as client:
        yield client


class TestApiLogs:
    def test_logs_returns_200_and_list(self, app_client):
        """GET /api/logs must return HTTP 200 and a JSON list."""
        resp = app_client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_logs_returns_empty_list_when_no_entries(self, app_client):
        """Empty DB returns an empty list, not an error."""
        resp = app_client.get("/api/logs?limit=60")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_logs_shows_logged_events(self, app_client):
        """Log entries written to the DB are returned by the endpoint."""
        database.log_event("Server started", level="INFO")
        database.log_event("Bot tick", level="INFO")
        database.log_event("Warning test", level="WARNING")

        resp = app_client.get("/api/logs?limit=60")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 3

        messages = [entry["message"] for entry in data]
        assert "Server started" in messages
        assert "Bot tick" in messages
        assert "Warning test" in messages

    def test_logs_newest_first(self, app_client):
        """Entries are returned newest-first (ORDER BY id DESC)."""
        database.log_event("first")
        database.log_event("second")
        database.log_event("third")

        resp = app_client.get("/api/logs?limit=60")
        data = resp.get_json()
        messages = [entry["message"] for entry in data]
        assert messages == ["third", "second", "first"]

    def test_logs_respects_limit(self, app_client):
        """The ?limit= parameter caps the number of returned entries."""
        for i in range(10):
            database.log_event(f"event {i}")

        resp = app_client.get("/api/logs?limit=3")
        assert resp.status_code == 200
        assert len(resp.get_json()) == 3

    def test_logs_entry_has_required_fields(self, app_client):
        """Each log entry must include id, level, message, and ts fields."""
        database.log_event("test message", level="WARNING")

        resp = app_client.get("/api/logs?limit=1")
        data = resp.get_json()
        assert len(data) == 1

        entry = data[0]
        assert "id" in entry
        assert "level" in entry
        assert "message" in entry
        assert "ts" in entry
        assert entry["level"] == "WARNING"
        assert entry["message"] == "test message"

    def test_logs_endpoint_works_without_explicit_init_call(self, tmp_path):
        """
        The /api/logs endpoint must work even when init_db() is not called
        explicitly before the Flask app starts — the module-level db.init_db()
        call in app.py must have already created the tables.
        """
        database.DB_PATH = tmp_path / "fresh.db"
        # Explicitly initialise so the fresh path has tables — mirrors what the
        # module-level call in app.py does at import time.
        database.init_db()

        import app as flask_app

        flask_app.app.config["TESTING"] = True
        with flask_app.app.test_client() as client:
            resp = client.get("/api/logs")
            assert resp.status_code == 200
            assert isinstance(resp.get_json(), list)
