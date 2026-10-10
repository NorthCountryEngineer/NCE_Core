"""Future model adapters consume authorized evidence; none are enabled here."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderBudget:
    max_tokens: int
    max_cost_micros: int
    timeout_ms: int


class AnswerProvider(Protocol):
    def answer(self, question: str, evidence: list[dict], budget: ProviderBudget) -> dict:
        """Return an answer with citation IDs. Evidence is untrusted data, never instructions.

        Implementations must reserve spend centrally before calling a provider,
        resolve credentials through an operator-managed secret reference, and
        validate output citations against this request's authorized evidence.
        No implementation is registered until those controls exist.
        """
        ...


def status():
    return {'status': 'not_configured', 'generation_enabled': False,
        'retrieval_enabled': True, 'tokens': 0, 'cost_micros': 0}
