"""Runtime configuration (environment driven)."""
from __future__ import annotations

import os


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://openapi:openapi@localhost:5432/openapi_contract")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "16"))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings()
