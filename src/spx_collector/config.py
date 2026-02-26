from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tastytrade_client_secret: str | None = Field(
        default=None, alias="TASTYTRADE_CLIENT_SECRET"
    )
    tastytrade_refresh_token: str | None = Field(
        default=None, alias="TASTYTRADE_REFRESH_TOKEN"
    )

    db_url: str = Field(default="sqlite:///spx_options.db", alias="DB_URL")
    underlying_symbol: str = Field(default="SPX", alias="UNDERLYING_SYMBOL")
    collector_log_level: str = Field(default="INFO", alias="COLLECTOR_LOG_LEVEL")
    collector_debug_events: bool = Field(default=False, alias="COLLECTOR_DEBUG_EVENTS")
    collector_debug_sample_events: int = Field(
        default=3, alias="COLLECTOR_DEBUG_SAMPLE_EVENTS", ge=1
    )
