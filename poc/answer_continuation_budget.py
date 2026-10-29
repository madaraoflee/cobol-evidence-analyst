"""Persistent output allowances for one source-backed business answer.

The allowance is fixed when the first answer continuation is considered.
Repeated evidence, revisions and reconnects must reuse this object rather than
open a new budget. Seed it with the answer output already received at that point.
The caller retains the draft and its partial status when an allowance runs out.
"""

from collections.abc import Mapping


def _integer(value, *, minimum=0):
    return type(value) is int and value >= minimum


class AnswerContinuationBudget:
    schema_version = "answer-continuation-budget/v1"

    def __init__(self, *, source_characters, framework_characters,
                 max_output_tokens, max_request_bytes):
        values = {"source_characters": (source_characters, 0),
                  "framework_characters": (framework_characters, 0),
                  "max_output_tokens": (max_output_tokens, 1),
                  "max_request_bytes": (max_request_bytes, 1)}
        for name, (value, minimum) in values.items():
            if not _integer(value, minimum=minimum):
                raise ValueError(f"{name} must be an integer at least {minimum}.")
        self.source_characters = source_characters
        self.framework_characters = framework_characters
        self.max_output_tokens = max_output_tokens
        self.max_request_bytes = max_request_bytes
        # This is an output allowance, not an estimate of tokenizer usage.
        # Four characters per token leaves room for Chinese and English prose.
        self.output_window_characters = max_output_tokens * 4
        material = min(source_characters + framework_characters, max_request_bytes)
        self.max_output_characters = (self.output_window_characters
            + max(self.output_window_characters, (material + 1) // 2))
        # A provider that adds one character per length-limited response must
        # still stop. Use a smaller progress window for attempts: Chinese prose
        # can yield roughly one visible character per token. The four-character
        # content allowance above must not prematurely cap those continuations.
        self.request_window_characters = max_output_tokens
        self.max_output_requests = ((self.max_output_characters
            + self.request_window_characters - 1) // self.request_window_characters)
        self.output_characters = 0
        self.output_requests = 0
        self.no_progress = False
        self.output_ids = set()

    @property
    def stop_reason(self):
        if self.no_progress:
            return "answer_output_no_progress"
        if self.output_characters >= self.max_output_characters:
            return "answer_output_characters_exhausted"
        if self.output_requests >= self.max_output_requests:
            return "answer_output_requests_exhausted"
        return None

    def can_continue(self):
        return self.stop_reason is None

    def record_output(self, text, *, output_id=None, made_progress=True):
        """Count original provider text, including overlap removed from a draft.

        The initial answer and each later answer output share this allowance.
        An already received output may be recorded even after exhaustion: it
        remains real consumption, and the caller must retain useful text.
        Return False for an already recorded response, such as journal replay.
        """
        if not isinstance(text, str):
            raise ValueError("Output text must be a string.")
        if output_id is not None and (not isinstance(output_id, str) or not output_id):
            raise ValueError("Output ID must be a nonempty string or None.")
        if type(made_progress) is not bool:
            raise ValueError("Output progress must be a boolean.")
        if output_id is not None and output_id in self.output_ids:
            return False
        if output_id is not None:
            self.output_ids.add(output_id)
        self.output_characters += len(text)
        self.output_requests += 1
        self.no_progress = self.no_progress or not made_progress or not text.strip()
        return True

    def checkpoint(self):
        return {"schema": self.schema_version,
                "source_characters": self.source_characters,
                "framework_characters": self.framework_characters,
                "max_output_tokens": self.max_output_tokens,
                "max_request_bytes": self.max_request_bytes,
                "output_window_characters": self.output_window_characters,
                "request_window_characters": self.request_window_characters,
                "max_output_characters": self.max_output_characters,
                "max_output_requests": self.max_output_requests,
                "output_characters": self.output_characters,
                "output_requests": self.output_requests,
                "no_progress": self.no_progress,
                "output_ids": sorted(self.output_ids)}

    def restore(self, state, *, fallback_output_characters=0, fallback_output_requests=0):
        """Restore without enlarging allowances or forgetting consumed output.

        Older journals can seed consumption from the retained draft and answer
        round count. An explicit user continuation must also supply that seed.
        Saved limits can narrow, but never enlarge, the newly derived limits.
        """
        for name, value in (("fallback_output_characters", fallback_output_characters),
                            ("fallback_output_requests", fallback_output_requests)):
            if not _integer(value):
                raise ValueError(f"{name} must be a nonnegative integer.")
        self.output_characters = max(self.output_characters, fallback_output_characters)
        self.output_requests = max(self.output_requests, fallback_output_requests)
        if not isinstance(state, Mapping) or state.get("schema") != self.schema_version:
            return self
        for name in ("max_output_characters", "max_output_requests"):
            if _integer(state.get(name)):
                setattr(self, name, min(getattr(self, name), state[name]))
        for name in ("output_characters", "output_requests"):
            if _integer(state.get(name)):
                setattr(self, name, max(getattr(self, name), state[name]))
        self.no_progress = self.no_progress or state.get("no_progress") is True
        identifiers = state.get("output_ids")
        if isinstance(identifiers, list):
            self.output_ids.update(value for value in identifiers
                                   if isinstance(value, str) and value)
        self.output_requests = max(self.output_requests, len(self.output_ids))
        return self
