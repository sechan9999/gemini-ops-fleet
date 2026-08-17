"""Runtime configuration.

Everything the fleet needs to switch between a laptop and Cloud Run lives here.
Defaults are chosen so the whole system runs offline with no credentials: SQLite
instead of Cloud SQL, heuristic guardrails instead of Model Armor. Set the
corresponding environment variables to promote each piece to its cloud service.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


class Settings(BaseModel):
    """Resolved settings for one process."""

    # --- Model -------------------------------------------------------------
    # The hackathon requires Gemini 3.5 or newer. Pin an explicit version here
    # rather than relying on the alias so the submission is verifiable.
    model: str
    project_id: str
    location: str

    # --- Storage -----------------------------------------------------------
    # SQLite locally, Cloud SQL (Postgres) in the cloud. Retrieval uses pgvector
    # when the URL points at Postgres and a keyword index otherwise, so access
    # control is enforced in SQL either way.
    database_url: str

    # --- Guardrails --------------------------------------------------------
    model_armor_template: str

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def model_armor_enabled(self) -> bool:
        return bool(self.model_armor_template and self.project_id)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        model=os.environ.get("FLEET_MODEL", "gemini-flash-latest"),
        project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        database_url=os.environ.get("DATABASE_URL", "sqlite:///fleet.db"),
        model_armor_template=os.environ.get("MODEL_ARMOR_TEMPLATE_ID", ""),
    )
