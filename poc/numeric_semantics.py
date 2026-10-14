"""Bounded decimal receiving semantics with explicit compilation assumptions.

This module describes storage conversion, not a complete COBOL arithmetic
runtime. Binary capacity, edited pictures, floating point, compiler intermediate
precision and invalid stored data require a selected compilation profile.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, localcontext
import re


DECIMAL_RECEIVER_PROFILE = "decimal-receiver-v1"


@dataclass(frozen=True)
class NumericLayout:
    picture: str | None
    usage: str
    signed: bool
    integer_digits: int | None
    scale: int | None
    supported: bool
    diagnostics: tuple[str, ...] = ()


def numeric_layout(declaration: str) -> NumericLayout:
    text = declaration.upper()
    pic = re.search(r"\bPIC(?:TURE)?\s+(?:IS\s+)?([A-Z0-9()+.,*/$+-]+)", text)
    usage = re.search(r"\b(?:USAGE\s+(?:IS\s+)?)?(COMP-[1-5]|COMP|COMPUTATIONAL(?:-[1-5])?|BINARY|PACKED-DECIMAL|DISPLAY|INDEX)\b", text)
    mode = usage.group(1) if usage else "DISPLAY"
    if mode in {"COMP-3", "COMPUTATIONAL-3"}:
        mode = "PACKED-DECIMAL"
    if not pic:
        return NumericLayout(None, mode, False, None, None, False, ("numeric_picture_not_declared",))
    picture = pic.group(1).rstrip(".")
    signed = picture.startswith("S")
    body = picture[1:] if signed else picture
    if not re.fullmatch(r"9(?:\([1-9][0-9]*\))?(?:9(?:\([1-9][0-9]*\))?)*(?:V9(?:\([1-9][0-9]*\))?(?:9(?:\([1-9][0-9]*\))?)*)?", body):
        return NumericLayout(picture, mode, signed, None, None, False,
                             ("edited_scaled_or_nonnumeric_picture_not_supported",))
    def count(part):
        return sum(int(match.group(1) or "1") for match in re.finditer(r"9(?:\(([1-9][0-9]*)\))?", part))
    integer, _, fractional = body.partition("V")
    integer_digits, scale = count(integer), count(fractional)
    diagnostics = []
    if integer_digits + scale > 31:
        diagnostics.append("picture_precision_requires_compilation_profile")
    if mode not in {"DISPLAY", "PACKED-DECIMAL"}:
        diagnostics.append("usage_capacity_or_precision_requires_compilation_profile")
    return NumericLayout(picture, mode, signed, integer_digits, scale, not diagnostics, tuple(diagnostics))


def receiving_contract(declaration: str, *, rounded=False, on_size_error=False, operation="COMPUTE") -> dict:
    layout = numeric_layout(declaration)
    return {"layout": asdict(layout), "operation": operation,
            "rounded": bool(rounded), "receiving_scale": layout.scale,
            "fractional_conversion": "nearest_away_from_zero" if rounded else "toward_zero",
            "size_error_check": "after_rounding" if rounded else "after_decimal_alignment",
            "on_size_error_receiver": "preserved" if on_size_error else "truncation_profile_required",
            "profile": DECIMAL_RECEIVER_PROFILE, "profile_confirmed": False,
            "status": "receiver_layout_supported" if layout.supported else "unsupported",
            "runtime_inputs_known": False, "intermediate_precision_verified": False}


def store_decimal(value, declaration: str, *, rounded=False, on_size_error=False,
                  operation="COMPUTE", profile=None) -> dict:
    """Convert an explicitly supplied decimal under a selected receiver profile.

    It never reads live table data or interprets bytes. Callers must separately
    establish the arithmetic expression's intermediate precision and inputs.
    """
    contract = receiving_contract(declaration, rounded=rounded, on_size_error=on_size_error, operation=operation)
    layout = numeric_layout(declaration)
    if not layout.supported or profile != DECIMAL_RECEIVER_PROFILE:
        return {**contract, "status": "unsupported", "value": None,
                "reason": "receiver_profile_not_confirmed" if profile != DECIMAL_RECEIVER_PROFILE else "receiver_layout_not_supported"}
    if isinstance(value, float) or not isinstance(value, (Decimal, str, int)):
        return {**contract, "status": "unsupported", "value": None, "reason": "exact_decimal_input_required"}
    try:
        number = Decimal(value)
    except InvalidOperation:
        return {**contract, "status": "unsupported", "value": None, "reason": "invalid_decimal_input"}
    if not number.is_finite() or len(number.as_tuple().digits) > 1000 or abs(number.adjusted()) > 1000:
        return {**contract, "status": "unsupported", "value": None, "reason": "decimal_input_outside_budget"}
    with localcontext() as context:
        context.prec = max(64, len(number.as_tuple().digits) + abs(number.adjusted()) + layout.scale + 4)
        quantum = Decimal(1).scaleb(-layout.scale)
        converted = number.quantize(quantum, rounding=ROUND_HALF_UP if rounded else ROUND_DOWN)
        limit = Decimal(10) ** layout.integer_digits
        overflow = abs(converted) >= limit
        arithmetic = operation != "MOVE"
        if overflow and arithmetic and on_size_error:
            return {**contract, "profile_confirmed": True, "status": "size_error", "value": None,
                    "receiver_preserved": True, "size_error": True, "input_value": str(number)}
        sign = -1 if converted.is_signed() and layout.signed else 1
        stored = (abs(converted) % limit) * sign
        return {**contract, "profile_confirmed": True, "status": "converted", "value": format(stored, f".{layout.scale}f"),
                "receiver_preserved": False, "size_error": overflow and arithmetic,
                "integer_truncated": overflow, "fractional_changed": converted != number,
                "input_value": str(number)}
