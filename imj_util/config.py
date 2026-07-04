from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    test_image_url: str
    gigachat_basic_auth: str
    gigachat_verify_ssl: bool
    gigachat_oauth_url: str
    gigachat_chat_url: str
    gigachat_scope: str
    gigachat_vision_model: str
    gigachat_timeout: float
    image_download_timeout: float
    database_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        project_root = Path(__file__).resolve().parent.parent
        db_path = os.getenv("DATABASE_PATH", "data/analyses.db")
        return cls(
            test_image_url=os.getenv("TEST_IMAGE_URL", ""),
            gigachat_basic_auth=os.getenv("GIGACHAT_BASIC_AUTH", ""),
            gigachat_verify_ssl=_env_bool("GIGACHAT_VERIFY", False),
            gigachat_oauth_url=os.getenv(
                "GIGACHAT_OAUTH_URL",
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            ),
            gigachat_chat_url=os.getenv(
                "GIGACHAT_CHAT_URL",
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            ),
            gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            gigachat_vision_model=os.getenv("GIGACHAT_VISION_MODEL", "GigaChat-Pro"),
            gigachat_timeout=float(os.getenv("GIGACHAT_TIMEOUT", "60")),
            image_download_timeout=float(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "120")),
            database_path=project_root / db_path,
        )

    def validate(self) -> None:
        if not self.gigachat_basic_auth:
            raise ValueError("GIGACHAT_BASIC_AUTH не задан в .env")


settings = Settings.from_env()
