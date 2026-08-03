from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


def load_local_env(env_path: Path | None = None) -> bool:
    """Load local process settings without replacing explicitly exported values."""
    path = env_path or Path.cwd() / ".env"
    return load_dotenv(dotenv_path=path, override=False)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Process configuration whose storage root owns every managed path."""

    storage_root: Path

    def __post_init__(self) -> None:
        root = self.storage_root.expanduser()
        if not root.is_absolute():
            raise ValueError("storage_root must be an absolute path")
        object.__setattr__(self, "storage_root", root.resolve())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        values = os.environ if env is None else env
        configured = values.get("FLYBOARD_STORAGE_ROOT")
        if configured is None:
            raise ValueError(
                "FLYBOARD_STORAGE_ROOT is required when --storage-root is not provided"
            )
        if not configured.strip():
            raise ValueError("FLYBOARD_STORAGE_ROOT must not be empty")
        return cls(Path(configured))
