"""LLM integration — Phase 1 mock for Ollama + Qwen3 8B Instruct.

Real implementation (Phase 4, per docs/llm.md) will call Ollama's HTTP API
(`OLLAMA_BASE_URL`) via `ChatOllama`/native tool calling. It MUST expose the
same `complete()` interface used here so LangGraph agent code written
against the mock keeps working unchanged once Ollama is wired in.

Switching to real Ollama later:
  1. Install Ollama (see docs/llm.md).
  2. `ollama pull qwen3:8b`.
  3. Set USE_MOCK_LLM=false in .env.
  4. Implement `RealLLMService` here and return it from `get_llm_service()`.
"""
import random
import time
from abc import ABC, abstractmethod

from app.config import get_settings

_REASONS = [
    "Zone {zone} is running {delta}°C below setpoint during low occupancy; "
    "raising the cooling setpoint by 0.5°C should cut HVAC load without "
    "affecting comfort (PMV stays within ±0.5).",
    "Outdoor temperature has dropped; pre-cooling is no longer required. "
    "Reducing fan speed will lower energy use while occupancy is at {occ} people.",
    "Occupancy in {zone} is trending down for the next interval; scheduling "
    "a setback now will not impact comfort category A.",
    "Solar gain on the west facade is increasing indoor temperature; "
    "recommend adjusting the cooling setpoint proactively before it drifts.",
]


class LLMServiceBase(ABC):
    @abstractmethod
    def complete(self, prompt: str, system: str | None = None) -> dict:
        ...

    @abstractmethod
    def status(self) -> dict:
        ...


class MockLLMService(LLMServiceBase):
    """Returns plausible reasoning text + confidence without calling any
    model — proves the agent/API contract before Ollama is installed."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def complete(self, prompt: str, system: str | None = None) -> dict:
        start = time.time()
        time.sleep(random.uniform(0.15, 0.4))  # simulate inference latency
        template = random.choice(_REASONS)
        text = template.format(
            zone=random.choice(["Core_ZN", "Perimeter_S_ZN", "Perimeter_W_ZN"]),
            delta=round(random.uniform(0.5, 2.0), 1),
            occ=random.randint(2, 15),
        )
        latency_ms = round((time.time() - start) * 1000, 1)
        return {
            "text": text,
            "confidence": round(random.uniform(0.72, 0.96), 2),
            "latency_ms": latency_ms,
            "token_count": len(text.split()) + len((prompt or "").split()),
        }

    def status(self) -> dict:
        return {
            "status": "mock",
            "model": f"{self.settings.llm_model} (mock)",
            "url": self.settings.ollama_base_url,
        }


_service_instance: LLMServiceBase | None = None


def get_llm_service() -> LLMServiceBase:
    global _service_instance
    if _service_instance is None:
        settings = get_settings()
        if settings.use_mock_llm:
            _service_instance = MockLLMService()
        else:
            # Phase 4+: return RealLLMService() (ChatOllama-backed) here.
            raise NotImplementedError("Real Ollama integration is implemented in Phase 4.")
    return _service_instance
