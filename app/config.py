"""Runtime configuration.

Everything the fleet needs to switch between a laptop and Cloud Run lives here.
Defaults are chosen so the whole system runs offline with no credentials: SQLite
instead of Cloud SQL, heuristic guardrails instead of Model Armor. Set the
corresponding environment variables to promote each piece to its cloud service.
"""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import quote_plus

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


def _resolve_database_url() -> str:
    """Build the connection string, preferring Cloud SQL when it is configured.

    On Cloud Run the instance is reached over a unix socket that the runtime
    mounts at /cloudsql, so there is no host or port and no password in any
    environment variable other than the one Secret Manager injects. An explicit
    DATABASE_URL still wins, which is what keeps local runs on SQLite.
    """
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    instance = os.environ.get("INSTANCE_CONNECTION_NAME")
    if not instance:
        return "sqlite:///fleet.db"

    user = os.environ.get("DB_USER", "fleet")
    password = quote_plus(os.environ.get("DB_PASSWORD", ""))
    name = os.environ.get("DB_NAME", "fleet")
    socket = f"/cloudsql/{instance}/.s.PGSQL.5432"
    return f"postgresql+pg8000://{user}:{password}@/{name}?unix_sock={socket}"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        # Pinned rather than aliased: the hackathon requires Gemini 3.5 or newer,
        # and `gemini-flash-latest` resolves to whatever is current, which proves
        # nothing to a judge reading the repo.
        model=os.environ.get("FLEET_MODEL", "gemini-3.5-flash"),
        project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        database_url=_resolve_database_url(),
        model_armor_template=os.environ.get("MODEL_ARMOR_TEMPLATE_ID", ""),
    )
