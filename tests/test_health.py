"""
Tests for the /health endpoint, including its failure path.
"""


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert "version" in body


def test_health_returns_503_when_database_is_down(client, monkeypatch):
    """A broken DB must produce a clean 503, not a server error."""
    import main

    class BrokenSession:
        def execute(self, *args, **kwargs):
            raise RuntimeError("could not connect to db.internal.example:5432")

        def close(self):
            pass

    monkeypatch.setattr(main, "SessionLocal", lambda: BrokenSession())

    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    # Internal error details must not leak to the caller
    assert "db.internal" not in r.text