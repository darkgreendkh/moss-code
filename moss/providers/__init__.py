"""Model provider adapters."""

from .clients import AnthropicCompatibleModelClient, FakeModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .capabilities import ModelCapabilities, capabilities_for

__all__ = [
    "AnthropicCompatibleModelClient",
    "FakeModelClient",
    "OllamaModelClient",
    "OpenAICompatibleModelClient",
    "ModelCapabilities",
    "capabilities_for",
]
