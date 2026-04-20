"""
Runtime configuration. Values come from environment variables so the same
code can run locally (thesis mode with OAM enabled) and in production
(user-test mode with OAM hidden).

Read values once at import time and expose them as module constants.
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Feature flag: enable thesis/OAM routes and UI panels.
# Default False for the user-facing test build.
# Set ENABLE_OAM_FEATURES=true to re-enable in dev / thesis mode.
ENABLE_OAM_FEATURES: bool = _env_bool("ENABLE_OAM_FEATURES", default=True)

# Max accepted upload size. 25 MB is plenty for image compression benchmarks
# and rejects accidental video uploads gracefully.
MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_BYTES", 25 * 1024 * 1024))

# CORS origins. Default "*" for development; set to the deployed frontend
# origin in production.
CORS_ORIGINS: list[str] = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
]
