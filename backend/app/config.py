from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Spin Scout API"
    database_url: str = "postgresql+psycopg://postgres:postgres@db:5432/spinscout"
    frontend_url: str = "http://localhost:5173"
    routing_provider: str = "graphhopper"
    graphhopper_api_key: str = ""
    graphhopper_base_url: str = "https://graphhopper.com/api/1"
    overpass_api_url: str = "https://overpass-api.de/api/interpreter"
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_redirect_uri: str = "http://localhost:8000/api/auth/strava/callback"
    strava_scopes: str = "read,activity:read_all"
    app_secret: str = "change-me"


settings = Settings()
