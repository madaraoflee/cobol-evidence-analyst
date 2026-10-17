"""Provider-independent resource allowances for an interactive investigation.

Ordinary excerpts and a uniquely resolved whole root have separate allowances.
Changing these values is a deployment choice, never an automatic response to a
difficult question.

Investigations finish according to evidence and answer quality by default.
``max_model_requests`` and ``max_question_seconds`` remain optional deployment
limits for compatibility. The latter is a completion target: after it, prefer
answering over optional investigation without cancelling a provider response.
``hard_question_seconds`` is a separate optional runtime limit. Transport
timeouts remain independent of all three.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class AgentPolicy:
    max_model_requests: int | None = None
    max_question_seconds: int | None = None
    max_source_characters: int = 36000
    max_complete_source_characters: int = 128000
    max_history_characters: int = 18000
    max_request_bytes: int = 230000
    max_searches_per_turn: int = 3
    max_reads_per_turn: int = 3
    max_framework_searches_per_turn: int = 3
    initial_pages: int = 6
    initial_source_characters: int = 24000
    search_pages: int = 8
    search_source_characters: int = 32000
    read_source_characters: int = 12000
    max_framework_references: int = 10
    max_framework_characters: int = 8000
    max_business_context_actions_per_turn: int = 2
    max_semantic_files: int = 32
    max_semantic_source_bytes: int = 33554432
    max_semantic_expansions: int = 4
    max_evidence_groups: int = 12
    max_initial_context_seconds: int = 3
    max_local_analysis_seconds: int = 15
    max_answer_revisions: int = 1
    semantic_cache_bytes: int = 536870912
    hard_question_seconds: int | None = None

    def __post_init__(self):
        optional_limits = {
            "max_model_requests": (1, 32),
            "max_question_seconds": (1, 1800),
            "hard_question_seconds": (1, 1800),
        }
        for name, (minimum, maximum) in optional_limits.items():
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not minimum <= value <= maximum):
                raise ValueError(
                    f"Agent policy {name} must be None or an integer from {minimum} to {maximum}.")
        limits = {
            "max_source_characters": (512, 128000),
            "max_complete_source_characters": (512, 128000),
            "max_history_characters": (0, 128000),
            # Leave room below the adapter's 256 KB transport hard limit.
            "max_request_bytes": (32768, 230000),
            "max_searches_per_turn": (0, 16),
            "max_reads_per_turn": (0, 16),
            "max_framework_searches_per_turn": (0, 16),
            "initial_pages": (1, 32),
            "initial_source_characters": (512, 128000),
            "search_pages": (1, 32),
            "search_source_characters": (512, 128000),
            "read_source_characters": (512, 128000),
            "max_framework_references": (1, 10),
            "max_framework_characters": (512, 8000),
            "max_business_context_actions_per_turn": (0, 8),
            "max_semantic_files": (1, 256),
            "max_semantic_source_bytes": (1024, 536870912),
            "max_semantic_expansions": (0, 16),
            "max_evidence_groups": (0, 64),
            "max_initial_context_seconds": (1, 120),
            "max_local_analysis_seconds": (1, 300),
            "max_answer_revisions": (0, 1),
            "semantic_cache_bytes": (1048576, 4294967296),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Agent policy {name} must be an integer from {minimum} to {maximum}.")

    def to_dict(self):
        return asdict(self)

    def remaining_model_requests(self, used):
        """Return an explicit remaining allowance, or None for adaptive runs."""
        if self.max_model_requests is None:
            return None
        return max(0, self.max_model_requests - used)

    def can_request(self, used):
        remaining = self.remaining_model_requests(used)
        return remaining is None or remaining > 0

    def is_last_request(self, used):
        return self.remaining_model_requests(used) == 1


def resolve_agent_policy(value=None):
    if value is None:
        return AgentPolicy()
    if isinstance(value, AgentPolicy):
        return value
    if not isinstance(value, Mapping) or set(value) - {item.name for item in fields(AgentPolicy)}:
        raise ValueError("Agent policy must contain supported configuration fields.")
    return AgentPolicy(**dict(value))
