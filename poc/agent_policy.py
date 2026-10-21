"""Provider-independent resource allowances for an interactive investigation.

Ordinary excerpts and a uniquely resolved whole root have separate allowances.
Changing these values is a deployment choice, never an automatic response to a
difficult question.

Investigation mode and source memory are independent of resource limits.
Request, investigation and revision counts remain finite by default. Total
question time is optional; an explicit deployment deadline remains binding.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class AgentPolicy:
    max_model_requests: int | None = 6
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
    # File/byte/action limits bound local work unless a deployment opts into
    # an additional elapsed-time cutoff.
    max_initial_context_seconds: int | None = None
    max_local_analysis_seconds: int | None = None
    # Keep the legacy 0/1 field separate from the new counted allowance.
    max_answer_revisions: int | None = None
    semantic_cache_bytes: int = 536870912
    hard_question_seconds: int | None = None
    # Optional synthesis target; never creates or extends a hard deadline.
    delivery_target_seconds: int | None = None
    investigation_mode: str = "adaptive"
    source_memory_enabled: bool = True
    max_investigation_rounds: int = 5
    max_no_evidence_revisions: int = 2
    finalization_reserve_seconds: int = 45

    def __post_init__(self):
        optional_limits = {
            "max_model_requests": (1, 32),
            "max_question_seconds": (1, 1800),
            "hard_question_seconds": (1, 1800),
            "delivery_target_seconds": (1, 1800),
            "max_initial_context_seconds": (1, 120),
            "max_local_analysis_seconds": (1, 300),
            "max_answer_revisions": (0, 1),
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
            "max_investigation_rounds": (0, 5),
            "max_no_evidence_revisions": (0, 2),
            "finalization_reserve_seconds": (0, 180),
            "semantic_cache_bytes": (1048576, 4294967296),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Agent policy {name} must be an integer from {minimum} to {maximum}.")
        if self.investigation_mode not in {"adaptive", "single_pass"}:
            raise ValueError("Agent policy investigation_mode must be adaptive or single_pass.")
        if type(self.source_memory_enabled) is not bool:
            raise ValueError("Agent policy source_memory_enabled must be a boolean.")
        # A missing request limit still uses the finite interactive allowance.
        # A missing time limit leaves progress and call counts in control.
        # Explicit legacy targets retain their existing hard-deadline meaning.
        object.__setattr__(self, "max_model_requests", self.max_model_requests or 6)
        deadlines = [value for value in (self.max_question_seconds, self.hard_question_seconds)
                     if value is not None]
        hard = min(*deadlines, 300) if deadlines else None
        object.__setattr__(self, "max_question_seconds", hard)
        object.__setattr__(self, "hard_question_seconds", hard)

    @property
    def effective_delivery_target_seconds(self):
        target = self.delivery_target_seconds if self.delivery_target_seconds is not None else self.max_question_seconds
        if target is None or self.hard_question_seconds is None:
            return target
        return min(target, self.hard_question_seconds)

    @property
    def answer_revision_limit(self):
        if self.max_answer_revisions is None:
            return self.max_no_evidence_revisions
        return min(self.max_answer_revisions, self.max_no_evidence_revisions)

    def to_dict(self):
        return asdict(self)

    def remaining_model_requests(self, used):
        """Count every provider attempt against the same question allowance."""
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
