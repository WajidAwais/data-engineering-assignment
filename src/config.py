"""Load config/settings.toml (gitignored, local secrets) and config/sources.toml
(committed, source endpoints) into one Settings object.

Every ingest/geo module should go through this rather than reading TOML itself.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _load_toml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config/settings.example.toml to "
            "config/settings.toml and fill in your keys."
        )
    with path.open("rb") as f:
        return tomllib.load(f)


@dataclass(frozen=True)
class Settings:
    census_api_key: str
    socrata_app_token: str
    default_start_date: str
    output_dir: str
    sources: dict

    @property
    def data_dir(self) -> Path:
        """Root for all generated layers. Gitignored; see .gitignore."""
        p = Path(self.output_dir)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def warehouse_path(self) -> Path:
        return self.data_dir / "warehouse.duckdb"


def load_settings() -> Settings:
    settings = _load_toml("settings.toml")
    sources = _load_toml("sources.toml")
    keys = settings.get("keys", {})
    run = settings.get("run", {})
    return Settings(
        census_api_key=keys.get("census_api_key", ""),
        socrata_app_token=keys.get("socrata_app_token", ""),
        default_start_date=run.get("default_start_date", ""),
        output_dir=run.get("output_dir", "data"),
        sources=sources,
    )
