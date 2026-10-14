"""Parser-neutral source, field, storage and statement contracts.

Syntax recognition, unique symbol binding and established flow are separate
states. An array index or uncertain layout never silently becomes an exact byte
range. Arithmetic literals remain decimal strings, not binary floating point.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from statement_facts import data_references


IR_VERSION = "semantic-ir-v1"


@dataclass(frozen=True)
class SourceRef:
    relative_path: str
    source_sha256: str
    start_line: int
    end_line: int
    start_column: int = 1
    end_column: int | None = None
    copy_instance: str | None = None
    include_chain: tuple[dict, ...] = ()
    replacement_trace: tuple[dict, ...] = ()
    coordinate_status: str = "physical"


@dataclass(frozen=True)
class DataItem:
    item_id: str
    name: str
    level: int
    ancestors: tuple[str, ...]
    parent_id: str | None
    declaration: str
    source_refs: tuple[SourceRef, ...]
    picture: str | None = None
    usage: str | None = None
    scale: int | None = None
    digits: int | None = None
    signed: bool | None = None
    occurs: tuple[int | None, int | None] | None = None
    depending_on: str | None = None
    redefines: str | None = None
    value: str | None = None
    declaration_status: str = "parsed"
    compilation_status: str = "active"
    diagnostics: tuple[str, ...] = ()

    @property
    def qualified_name(self):
        return self.name + "".join(" OF " + name for name in self.ancestors)


@dataclass(frozen=True)
class DataRef:
    name: str
    qualifiers: tuple[str, ...] = ()
    subscripts: tuple[str, ...] = ()
    reference_modification: tuple[str, str] | None = None
    candidates: tuple[str, ...] = ()
    binding_status: str = "unbound"
    source_refs: tuple[SourceRef, ...] = ()

    @property
    def identity(self):
        result = self.name + "".join(" OF " + name for name in self.qualifiers)
        if self.subscripts:
            result += "(" + ", ".join(self.subscripts) + ")"
        if self.reference_modification:
            result += "(" + ":".join(self.reference_modification) + ")"
        return result


@dataclass(frozen=True)
class StorageRegion:
    region_id: str
    item_id: str
    root_id: str
    parent_region_id: str | None
    overlaps: tuple[str, ...] = ()
    array: bool = False
    offset_bytes: int | None = None
    length_bytes: int | None = None
    layout_status: str = "conservative"
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class Expression:
    kind: str
    value: str | None = None
    operands: tuple[Expression, ...] = ()
    support_status: str = "parsed"


@dataclass(frozen=True)
class ParsedStatement:
    occurrence_id: str
    rule_shape_id: str
    statement_type: str
    text: str
    source_refs: tuple[SourceRef, ...]
    reads: tuple[DataRef, ...] = ()
    writes: tuple[DataRef, ...] = ()
    expressions: tuple[tuple[str, Expression], ...] = ()
    scope: tuple[dict, ...] = ()
    parsed_status: str = "parsed"
    binding_status: str = "unbound"
    flow_status: str = "not_established"
    rounded: bool = False
    effects_unknown: bool = False
    compilation_status: str = "active"
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompilationIR:
    context: dict
    adapter: dict
    data_items: tuple[DataItem, ...] = ()
    storage_regions: tuple[StorageRegion, ...] = ()
    statements: tuple[ParsedStatement, ...] = ()
    diagnostics: tuple[dict, ...] = ()
    parse_complete: bool = False
    schema_version: str = IR_VERSION

    def to_dict(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return digest(self.to_dict())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def bind_reference(reference, items, source_refs=()):
    lexical = data_references(reference)
    if len(lexical) != 1:
        return DataRef(reference, binding_status="unsupported", source_refs=tuple(source_refs))
    ref = lexical[0]
    candidates = []
    for item in items:
        if item.name != ref.name:
            continue
        position = 0
        for qualifier in ref.qualifiers:
            try:
                position = item.ancestors.index(qualifier, position) + 1
            except ValueError:
                break
        else:
            candidates.append(item.item_id)
    uncertain = any(item.item_id in candidates and item.compilation_status != "active" for item in items)
    return DataRef(ref.name, ref.qualifiers, ref.subscripts, ref.reference_modification,
                   tuple(candidates), "candidate" if uncertain else "bound" if len(candidates) == 1 else
                   "ambiguous" if candidates else "unresolved", tuple(source_refs))


def compilation_ir_from_dict(document):
    """Convert a JSON contract, never retain implementation-specific objects."""
    if document.get("schema_version") != IR_VERSION:
        raise ValueError("SEMANTIC_IR_SCHEMA_MISMATCH")

    def source(row):
        values = dict(row)
        for key in ("include_chain", "replacement_trace"):
            values[key] = tuple(values.get(key, ()))
        return SourceRef(**values)

    def reference(row):
        values = dict(row)
        for key in ("qualifiers", "subscripts", "candidates"):
            values[key] = tuple(values.get(key, ()))
        values["source_refs"] = tuple(source(ref) for ref in values.get("source_refs", ()))
        if values.get("reference_modification"):
            values["reference_modification"] = tuple(values["reference_modification"])
        return DataRef(**values)

    def expression(row):
        values = dict(row)
        values["operands"] = tuple(expression(part) for part in values.get("operands", ()))
        if values.get("kind") == "decimal" and not isinstance(values.get("value"), str):
            raise ValueError("SEMANTIC_IR_DECIMAL_MUST_BE_TEXT")
        return Expression(**values)

    items = []
    for row in document.get("data_items", []):
        values = dict(row)
        values["source_refs"] = tuple(source(ref) for ref in values.get("source_refs", ()))
        values["ancestors"], values["diagnostics"] = tuple(values.get("ancestors", ())), tuple(values.get("diagnostics", ()))
        if values.get("occurs"):
            values["occurs"] = tuple(values["occurs"])
        items.append(DataItem(**values))
    regions = []
    for row in document.get("storage_regions", []):
        values = dict(row)
        for key in ("overlaps", "diagnostics"):
            values[key] = tuple(values.get(key, ()))
        regions.append(StorageRegion(**values))
    statements = []
    for row in document.get("statements", []):
        values = dict(row)
        values["source_refs"] = tuple(source(ref) for ref in values.get("source_refs", ()))
        for key in ("reads", "writes"):
            values[key] = tuple(reference(ref) for ref in values.get(key, ()))
        values["expressions"] = tuple((target, expression(tree)) for target, tree in values.get("expressions", ()))
        for key in ("scope", "diagnostics"):
            values[key] = tuple(values.get(key, ()))
        statements.append(ParsedStatement(**values))
    return CompilationIR(document["context"], document["adapter"], tuple(items), tuple(regions),
                         tuple(statements), tuple(document.get("diagnostics", ())),
                         bool(document.get("parse_complete")))
