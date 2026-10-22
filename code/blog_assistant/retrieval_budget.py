"""Retrieval allowance schema and recovery validation; no model or tool execution."""
import math
from dataclasses import asdict, dataclass
from .reference_context import validate_repair


@dataclass(frozen=True)
class RetrievalLimits:
    model_requests: int = 8
    tool_attempts: int = 12
    searches: int = 2
    file_searches: int = 2
    text_searches: int = 3
    no_progress: int = 2
    elapsed_seconds: int = 120
    context_bytes: int = 32000
    output_tokens: int = 8192

    def __post_init__(self):
        for key, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"Retrieval limit {key} must be a positive integer")


def validate_checkpoint(state):
    for turn in state["turns"]:
        validate_repair(turn)
        embedding = turn.get("embedding")
        if embedding is not None:
            if (not isinstance(embedding, dict) or embedding.get("version") != 1
                    or not isinstance(embedding.get("model"), str)
                    or any(type(embedding.get(k)) is not int or embedding[k] < 0
                           for k in ("batches", "texts", "max_batches", "max_texts"))
                    or embedding["batches"] > embedding["max_batches"]
                    or embedding["texts"] > embedding["max_texts"]
                    or embedding["external_cost"] != 0):
                raise ValueError("Invalid saved local embedding allowance")
        budget = turn.get("retrieval")
        if budget is None:
            continue
        if budget.get('phase', 'discovery') not in {'discovery', 'locating', 'reading', 'answering'}:
            raise ValueError('Invalid retrieval phase')
        if not isinstance(budget, dict) or type(budget.get("version")) is not int or budget.get("version") not in {1, 2}:
            raise ValueError("Unsupported retrieval checkpoint")
        try:
            limits = RetrievalLimits(**budget.get("limits", {}))
        except TypeError as exc:
            raise ValueError("Invalid retrieval checkpoint limits") from exc
        for key in ("model_requests", "tool_attempts", "searches", "no_progress", "context_peak_bytes"):
            if type(budget.get(key)) is not int or budget[key] < 0:
                raise ValueError("Invalid retrieval checkpoint counter")
        if (budget["model_requests"] > limits.model_requests or budget["tool_attempts"] > limits.tool_attempts
                or budget["searches"] > limits.searches or budget["context_peak_bytes"] > limits.context_bytes):
            raise ValueError("Retrieval checkpoint exceeds its saved limits")
        if budget["version"] == 2:
            if type(budget.get("recovery_used", False)) is not bool:
                raise ValueError("Invalid saved retrieval recovery flag")
            reserve = budget.get("finalization_reserve", 1)
            if type(reserve) is not int or reserve not in {1, 2} or reserve > limits.model_requests:
                raise ValueError("Invalid saved finalization reserve")
            for key in ("file_searches", "text_searches"):
                if (type(budget.get(key)) is not int or type(budget["limits"].get(key)) is not int
                        or not 0 <= budget[key] <= budget["limits"][key]):
                    raise ValueError("Invalid retrieval checkpoint category budget")
            if type(budget.get("output_retries")) is not int or not 0 <= budget["output_retries"] <= 1:
                raise ValueError("Invalid retrieval checkpoint retry counter")
            if type(budget.get("citation_retries", 0)) is not int or not 0 <= budget.get("citation_retries", 0) <= 1:
                raise ValueError("Invalid retrieval checkpoint citation retry counter")
        if (type(budget.get("deadline")) not in {int, float} or not math.isfinite(budget["deadline"])
                or type(budget.get("finalizing")) is not bool
                or budget.get("route") not in {"catalog", "qa", "help"}
                or (budget.get("reason") is not None and not isinstance(budget["reason"], str))
                or not isinstance(budget.get("cache"), dict)
                or not isinstance(budget.get("seen"), list)
                or not all(isinstance(i, str) for i in budget["seen"])):
            raise ValueError("Invalid retrieval checkpoint")

