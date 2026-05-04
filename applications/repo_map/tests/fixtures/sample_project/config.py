"""Global configuration for the sample project."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Settings:
    """Application settings — loaded once at startup."""
    db_url: str = "sqlite:///sample.db"
    debug: bool = False
    max_retries: int = 3
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])

    def is_production(self) -> bool:
        return not self.debug


@dataclass
class DatabaseConfig:
    """Database-specific configuration."""
    url: str = "sqlite:///sample.db"
    pool_size: int = 5
    echo: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "DatabaseConfig":
        return cls(url=settings.db_url, echo=settings.debug)
