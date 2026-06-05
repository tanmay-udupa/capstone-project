"""
Application settings — loaded from environment variables or .env file.

Production: set equivalent App Settings in Azure App Service (never commit .env).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Azure SQL ─────────────────────────────────────────────────────────────
    SQL_SERVER:   str
    SQL_DATABASE: str
    SQL_USERNAME: str
    SQL_PASSWORD: str

    # ── Azure DevOps ──────────────────────────────────────────────────────────
    ADO_ORG:      str
    ADO_PAT:      str = ""
    # Fixed ADO resource ID — do not change
    ADO_RESOURCE: str = "499b84ac-1321-427f-aa17-267ca6975798"

    # ── Azure OpenAI — LLM recommendation narratives ─────────────────────────
    AZURE_OPENAI_ENDPOINT:   str  = ""
    AZURE_OPENAI_API_KEY:    str  = ""
    AZURE_OPENAI_DEPLOYMENT: str  = "gpt-4o"
    AZURE_OPENAI_API_VERSION:str  = "2024-08-01-preview"
    # Flip to True once the LLM client is wired up in recommender.generate_narrative()
    LLM_ENABLED:             bool = False

    # ── Model artefacts ───────────────────────────────────────────────────────
    # Place xgb_best_model.pkl from the capstone repo into models/
    MODEL_PATH:                   str = "models/xgb_best_model.ubj"
    # Bump MODEL_VERSION whenever you re-register / retrain
    MODEL_VERSION:                str = "1"

    # ── Benchmark thresholds ──────────────────────────────────────────────────
    BENCHMARK_MIN_SAMPLES_HIGH:   int = 30   # >= 30 runs → high confidence
    BENCHMARK_MIN_SAMPLES_MEDIUM: int = 10   # 10-29 runs  → medium confidence


settings = Settings()
