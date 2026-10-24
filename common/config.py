"""All configuration comes from environment variables (read lazily, so tests can override)."""
import os


def store_api_url() -> str:
    return os.getenv("STORE_API_URL", "http://localhost:8001").rstrip("/")


def orders_agent_url() -> str:
    return os.getenv("ORDERS_AGENT_URL", "http://localhost:8002").rstrip("/")


def checkpoint_db_url() -> str:
    return os.getenv("CHECKPOINT_DB_URL", "sqlite:///./checkpoints.db")


def llm_provider() -> str:
    return os.getenv("LLM_PROVIDER", "fake").lower()


def llm_model() -> str:
    return os.getenv("LLM_MODEL", "")
