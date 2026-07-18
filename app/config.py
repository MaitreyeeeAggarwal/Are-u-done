from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    timezone: str = "Asia/Kolkata"
    owner_whatsapp_number: str
    public_base_url: str = "http://localhost:8000"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    validate_twilio_signature: bool = True
    supabase_url: str
    supabase_service_role_key: str
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_nano_model: str = "nvidia/nvidia-nemotron-nano-9b-v2"
    nvidia_super_model: str = "nvidia/nemotron-3-super-120b-a12b"
    worker_id: str = "worker-local"
    queue_lease_seconds: int = 120
    max_delivery_attempts: int = 5


@lru_cache
def settings() -> Settings:
    return Settings()
