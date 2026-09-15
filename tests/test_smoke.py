"""Phase 0 smoke test: proves venv + config wiring works end-to-end.

Not part of the graded suite - just confirms settings.toml/sources.toml load
and the network path to a public, no-key source is reachable before any real
ingestion logic is written.
"""

import requests

from src.config import load_settings


def test_config_loads():
    settings = load_settings()
    assert settings.sources["montgomery"]["incidents"] == "bhju-22kf"


def test_montgomery_endpoint_reachable():
    settings = load_settings()
    moco = settings.sources["montgomery"]
    url = f"{moco['base']}/{moco['incidents']}.json"
    resp = requests.get(url, params={"$limit": 1}, timeout=10)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
