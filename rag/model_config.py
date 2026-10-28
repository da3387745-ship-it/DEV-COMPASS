"""Small, standalone settings for OpenAI-compatible chat model APIs.

Set MODEL_API_KEY and MODEL_NAME in the environment, or fill in the optional
values when calling ``get_model_config``. Any model name is accepted, so new
models do not require code changes.
"""

import os
from dataclasses import dataclass


MODEL_API_KEY = ""
MODEL_NAME = ""
MODEL_BASE_URL = ""

PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
}


@dataclass(frozen=True)
class ModelConfig:
    """Connection details for one chat-completions-compatible model."""

    model: str
    api_key: str
    base_url: str


def get_model_config(
    provider: str = "",
    model: str = "",
    api_key: str = "",
    base_url: str = "",
) -> ModelConfig:
    """Build model settings from arguments, environment, or blank defaults.

    Pass any model identifier supported by the selected provider. For a
    provider not listed above, supply its API base URL explicitly.
    """
    selected_provider = provider.strip().lower()
    selected_model = (
        model.strip()
        or os.getenv("MODEL_NAME", "").strip()
        or MODEL_NAME
    )
    selected_key = (
        api_key.strip()
        or os.getenv("MODEL_API_KEY", "").strip()
        or MODEL_API_KEY
    )
    selected_url = (
        base_url.strip()
        or os.getenv("MODEL_BASE_URL", "").strip()
        or MODEL_BASE_URL
        or PROVIDER_BASE_URLS.get(selected_provider, "")
    )

    if not selected_model:
        raise ValueError("Set MODEL_NAME or pass a model name.")
    if not selected_key:
        raise ValueError("Set MODEL_API_KEY or pass an API key.")
    if not selected_url:
        raise ValueError(
            "Set MODEL_BASE_URL or pass a supported provider name and API URL."
        )

    return ModelConfig(
        model=selected_model,
        api_key=selected_key,
        base_url=selected_url.rstrip("/"),
    )
