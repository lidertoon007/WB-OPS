from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "WB Operations Service"
    environment: str = "development"
    dry_run: bool = True
    allow_remote: bool = False
    # Папка именно для отчётов поставок FBS. Документы Честного знака сюда не подмешиваются.
    fbs_report_root: str = "fbs_reports"
    # Старый ключ оставлен для совместимости с существующим .env; в работе не используется.
    report_root: str | None = None
    wb_api_base_url: str = "https://common-api.wildberries.ru"
    feedbacks_api_base_url: str = "https://feedbacks-api.wildberries.ru"
    auto_sync_enabled: bool = False
    auto_sync_interval_minutes: int = 15
    node_path: str = "node"
    database_url: str = "sqlite:///data/wbops.sqlite3"
    cabinets_json: str = "[{\"id\":\"default\",\"name\":\"Основной кабинет\",\"token\":\"\"}]"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="WBOPS_")


@lru_cache
def get_settings() -> Settings:
    return Settings()
