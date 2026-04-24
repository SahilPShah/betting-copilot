"""
Extension point for LLM-based confidence signals.

To add a new signal (e.g. injury severity, weather):
  1. Create a new file: llm/signals/your_signal.py
  2. Implement a class with:
       name: str
       def evaluate(self, game_context: dict) -> SignalResult | None
  3. In recs/run_recs.py, instantiate your signal and pass results to compute_confidence()

SignalResult fields:
  name  — identifier, e.g. "injury_severity", "weather"
  score — confidence modifier on a 1–10 scale
  note  — one-line human-readable justification shown in CLI output
"""

from typing import Protocol, runtime_checkable


class SignalResult:
    __slots__ = ("name", "score", "note")

    def __init__(self, name: str, score: float, note: str):
        self.name = name
        self.score = max(1.0, min(10.0, float(score)))
        self.note = note

    def __repr__(self):
        return f"SignalResult(name={self.name!r}, score={self.score}, note={self.note!r})"


@runtime_checkable
class LLMSignal(Protocol):
    name: str

    def evaluate(self, game_context: dict) -> "SignalResult | None":
        """
        Given a game_context dict, return a SignalResult or None if the signal
        is not applicable (e.g. no injury data available for this game).
        """
        ...
