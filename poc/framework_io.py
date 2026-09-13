"""Pure, bounded execution of explicitly declared record-access contracts.

This module neither opens business files nor predicts a production runtime.
Records, locks, failures and restart tokens are scenario inputs. Schema 1.0
defines an ordered, equality-filtered, read-your-own-writes model; cursor state
is identified by the declared access path and session, never a program name.
"""

from __future__ import annotations

from copy import deepcopy
from functools import wraps
import hashlib
import json
import re


MAX_RECORDS = 128
MAX_EVENTS = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9_$#@-]+\Z")
_ACTIONS = {"first", "next", "get", "save", "close", "commit", "rollback", "checkpoint", "resume"}


def _input_errors(function):
    """Malformed nested input has one public exception type, never a traceback leak."""
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (TypeError, KeyError, AttributeError, RecursionError, OverflowError) as error:
            raise ValueError("Malformed bounded runtime model input") from error
    return checked


def _object(value, required, optional, label):
    if not isinstance(value, dict) or required - value.keys() or value.keys() - required - optional:
        raise ValueError(f"Invalid {label} fields")
    return value


def _text(value, label, identifier=False):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(f"Invalid {label}")
    if identifier and not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label} identifier")
    return value


def _scalar(value, label):
    if type(value) not in (int, str) or (isinstance(value, str) and len(value) > 2048):
        raise ValueError(f"Invalid {label} scalar")
    if type(value) is int and abs(value) > 10**18 - 1:
        raise ValueError(f"Invalid {label} integer")
    return value


def _rows(value, label, maximum=32):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"Invalid {label} list")
    return value


def _unique(rows, key, label):
    if len({row[key] for row in rows}) != len(rows):
        raise ValueError(f"Duplicate {label}")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _typed(value, kind):
    return type(value) is (int if kind == "integer" else str)


@_input_errors
def validate_runtime_contract(contract: dict) -> dict:
    """Return a validated copy; configuration cannot assert runtime verification."""
    c = deepcopy(_object(contract, {"schema_version", "contract_id", "contract_version", "provenance", "dataset",
                                   "access_paths", "services", "statuses", "locking", "transaction", "restart"},
                         set(), "runtime contract"))
    if c["schema_version"] != "1.0":
        raise ValueError("Unsupported runtime contract schema_version")
    for name in ("contract_id", "contract_version"):
        _text(c[name], name)
    provenance = _object(c["provenance"], {"kind", "reference", "target_version"}, set(), "provenance")
    if provenance["kind"] not in {"synthetic", "public_candidate", "company_validated"}:
        raise ValueError("Unsupported provenance kind")
    for name in ("reference", "target_version"):
        _text(provenance[name], name)
    dataset = _object(c["dataset"], {"id", "fields", "primary_key"}, set(), "dataset")
    _text(dataset["id"], "dataset id", True)
    fields = dataset["fields"]
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 32:
        raise ValueError("Invalid dataset fields")
    for field, kind in fields.items():
        _text(field, "dataset field", True)
        if kind not in {"integer", "string"}:
            raise ValueError("Unsupported dataset field type")
    primary = _rows(dataset["primary_key"], "primary key")
    if not primary or len(set(primary)) != len(primary) or any(key not in fields for key in primary):
        raise ValueError("Invalid primary key")
    paths = _rows(c["access_paths"], "access paths", 16)
    if not paths:
        raise ValueError("At least one access path is required")
    for path in paths:
        _object(path, {"id", "key_fields", "select", "join"}, set(), "access path")
        _text(path["id"], "access path id", True)
        keys = _rows(path["key_fields"], "access path keys")
        if (not keys or len(set(keys)) != len(keys) or any(key not in fields for key in keys)
                or not set(primary) <= set(keys)):
            raise ValueError("Access path keys must include the complete unique primary key")
        if not isinstance(path["select"], dict) or set(path["select"]) - fields.keys():
            raise ValueError("Invalid access path equality filter")
        for key, value in path["select"].items():
            _scalar(value, "filter")
            if not _typed(value, fields[key]):
                raise ValueError("Filter type does not match field")
        if type(path["join"]) is not bool:
            raise ValueError("Access path join must be boolean")
    _unique(paths, "id", "access path")
    paths_by_id = {path["id"]: path for path in paths}
    statuses = _object(c["statuses"], {"ok", "eof", "not_found", "locked", "invalid"}, set(), "statuses")
    for status in statuses.values():
        _scalar(status, "status")
    if len({(type(value).__name__, value) for value in statuses.values()}) != len(statuses):
        raise ValueError("Outcome statuses must be distinct")
    services = _rows(c["services"], "services", 16)
    if not services:
        raise ValueError("At least one service is required")
    for service in services:
        _object(service, {"program", "argument", "function_field", "status_field", "session", "access_path",
                          "key_fields", "record_fields", "operations"}, set(), "service")
        for name in ("program", "argument", "function_field", "status_field", "session"):
            _text(service[name], name, True)
        for name in ("program", "argument", "function_field", "status_field"):
            service[name] = service[name].upper()
        if service["access_path"] not in paths_by_id:
            raise ValueError("Unknown service access path")
        if not isinstance(service["key_fields"], dict) or set(service["key_fields"]) != set(paths_by_id[service["access_path"]]["key_fields"]):
            raise ValueError("Service key fields must bind the complete access path key")
        if not isinstance(service["record_fields"], dict) or set(service["record_fields"]) != set(fields):
            raise ValueError("Service record fields must bind every dataset field")
        for mapping in (service["key_fields"], service["record_fields"]):
            for field, name in mapping.items():
                mapping[field] = _text(name, "source field", True).upper()
            if len(set(mapping.values())) != len(mapping):
                raise ValueError("Overlapping source record fields are unsupported")
        data_names = set(service["key_fields"].values()) | set(service["record_fields"].values())
        if service["function_field"] == service["status_field"] or {service["function_field"], service["status_field"]} & data_names:
            raise ValueError("Function, status and record fields must be distinct")
        # A request key may also be the returned record key, but not another field.
        reverse = {name: field for field, name in service["record_fields"].items()}
        if any(name in reverse and reverse[name] != field for field, name in service["key_fields"].items()):
            raise ValueError("Request key overlaps a different record field")
        operations = _rows(service["operations"], "operations", 16)
        if not operations:
            raise ValueError("At least one operation is required")
        for operation in operations:
            _object(operation, {"value", "action"}, {"reads_record"}, "operation")
            _text(operation["value"], "function value")
            if operation["action"] not in _ACTIONS:
                raise ValueError("Unsupported operation action")
            if operation["action"] == "first":
                if type(operation.get("reads_record")) is not bool:
                    raise ValueError("FIRST requires an explicit reads_record contract")
            elif "reads_record" in operation:
                raise ValueError("reads_record is only declared for FIRST")
        _unique(operations, "value", "function value")
    _unique(services, "program", "service program")
    locking = _object(c["locking"], {"read", "save_requires_lock", "release_on_advance", "release_on_close"}, set(), "locking")
    if locking["read"] not in {"exclusive", "none"}:
        raise ValueError("Unsupported read lock policy")
    for name in ("save_requires_lock", "release_on_advance", "release_on_close"):
        if type(locking[name]) is not bool:
            raise ValueError("Lock policy must be boolean")
    if locking["save_requires_lock"] and locking["read"] != "exclusive":
        raise ValueError("Required write locks need an explicit acquisition policy")
    tx = _object(c["transaction"], {"mode", "scope", "read_visibility", "commit_cursor", "rollback_cursor",
                                   "commit_locks", "rollback_locks"}, set(), "transaction")
    if (tx["mode"] not in {"explicit", "autocommit"} or tx["scope"] != "all_services"
            or tx["read_visibility"] != "own_pending" or tx["commit_cursor"] not in {"preserve", "close"}
            or tx["rollback_cursor"] != "close" or tx["commit_locks"] not in {"release", "preserve"}
            or tx["rollback_locks"] != "release"):
        raise ValueError("Unsupported transaction policy")
    restart = _object(c["restart"], {"enabled", "checkpoint", "resume"}, set(), "restart")
    if type(restart["enabled"]) is not bool or restart["checkpoint"] != "committed_key" or restart["resume"] != "strictly_after":
        raise ValueError("Unsupported restart policy")
    return c


def _records(contract, records):
    fields = contract["dataset"]["fields"]
    checked = deepcopy(_rows(records, "scenario records", MAX_RECORDS))
    seen = set()
    for record in checked:
        _object(record, set(fields), set(), "record")
        for field, kind in fields.items():
            _scalar(record[field], "record field")
            if not _typed(record[field], kind):
                raise ValueError("Record field type does not match contract")
        key = tuple(record[field] for field in contract["dataset"]["primary_key"])
        if key in seen:
            raise ValueError("Duplicate scenario primary key")
        seen.add(key)
    return sorted(checked, key=lambda record: _primary(contract, record))


def _primary(contract, record):
    return tuple(record[field] for field in contract["dataset"]["primary_key"])


def _cursor_id(service):
    return service["access_path"] + ":" + service["session"]


def _seal(state):
    """An integrity checksum detects accidental edits; it is not authentication."""
    state["state_hash"] = _hash({key: value for key, value in state.items() if key != "state_hash"})
    return state


@_input_errors
def initial_io_state(contract: dict, scenario: dict) -> dict:
    """Validate bounded scenario data; supplied restart metadata remains a claim."""
    c = validate_runtime_contract(contract)
    scenario = deepcopy(_object(scenario, {"records"}, {"external_locks", "restart_checkpoint", "faults"}, "scenario"))
    records = _records(c, scenario["records"])
    external = _rows(scenario.get("external_locks", []), "external locks", MAX_RECORDS)
    seen_locks = set()
    for lock in external:
        _object(lock, {"key", "owner"}, set(), "external lock")
        _text(lock["owner"], "lock owner")
        key = _rows(lock["key"], "lock key")
        primary = c["dataset"]["primary_key"]
        if len(key) != len(primary) or any(not _typed(value, c["dataset"]["fields"][field]) for field, value in zip(primary, key)):
            raise ValueError("Invalid external lock key")
        if tuple(key) in seen_locks or tuple(key) not in {_primary(c, record) for record in records}:
            raise ValueError("Duplicate or missing externally locked record")
        seen_locks.add(tuple(key))
    faults = _rows(scenario.get("faults", []), "scenario faults", 64)
    seen_faults = set()
    programs = {service["program"]: service for service in c["services"]}
    for fault in faults:
        _object(fault, {"program", "function", "occurrence", "outcome"}, {"status"}, "fault")
        fault["program"] = _text(fault["program"], "fault program", True).upper()
        if fault["program"] not in programs or fault["function"] not in {op["value"] for op in programs[fault["program"]]["operations"]}:
            raise ValueError("Unknown fault service or exact function value")
        if type(fault["occurrence"]) is not int or not 1 <= fault["occurrence"] <= MAX_EVENTS:
            raise ValueError("Invalid fault occurrence")
        if fault["outcome"] == "status":
            status = fault.get("status")
            if type(status) not in (int, str) or status not in [value for key, value in c["statuses"].items() if key != "ok"]:
                raise ValueError("Fault requires a declared non-success status")
        elif fault["outcome"] != "exception" or "status" in fault:
            raise ValueError("Invalid fault outcome")
        identity = (fault["program"], fault["function"], fault["occurrence"])
        if identity in seen_faults:
            raise ValueError("Duplicate scenario fault")
        seen_faults.add(identity)
    checkpoint = scenario.get("restart_checkpoint")
    if checkpoint is not None:
        _object(checkpoint, {"schema_version", "contract_hash", "contract_version", "dataset_hash", "access_path", "key"}, set(), "checkpoint")
        for name in ("schema_version", "contract_hash", "contract_version", "dataset_hash", "access_path"):
            _text(checkpoint[name], "checkpoint " + name)
        for value in _rows(checkpoint["key"], "checkpoint key"):
            _scalar(value, "checkpoint key")
    return _seal({"schema_version": "1.0", "contract_hash": _hash(c), "runtime_verified": False,
            "committed_records": records, "pending_writes": [], "cursors": {}, "locks": [],
            "external_locks": external, "committed_progress": {}, "restart_checkpoint": checkpoint,
            "faults": faults, "call_counts": {}, "event_count": 0})


def _state_shape(contract, state):
    """Even a recomputed checksum cannot make an unbounded model state valid."""
    _object(state, {"schema_version", "contract_hash", "runtime_verified", "committed_records",
                   "pending_writes", "cursors", "locks", "external_locks", "committed_progress",
                   "restart_checkpoint", "faults", "call_counts", "event_count", "state_hash"}, set(), "state")
    if state["schema_version"] != "1.0" or type(state["event_count"]) is not int or not 0 <= state["event_count"] <= MAX_EVENTS:
        raise ValueError("Invalid model state version or event count")
    initial_io_state(contract, {"records": state["committed_records"], "external_locks": state["external_locks"],
                               "restart_checkpoint": state["restart_checkpoint"], "faults": state["faults"]})
    pending = _records(contract, state["pending_writes"])
    committed = {_primary(contract, row): row for row in state["committed_records"]}
    ordering_fields = {field for path in contract["access_paths"] for field in path["key_fields"]}
    if any(_primary(contract, row) not in committed or any(row[field] != committed[_primary(contract, row)][field]
           for field in ordering_fields) for row in pending):
        raise ValueError("Pending model write changes record identity or ordering")
    paths = {path["id"]: path for path in contract["access_paths"]}
    identities = {_cursor_id(service): service for service in contract["services"]}

    def key_shape(key, fields):
        if (not isinstance(key, list) or len(key) != len(fields)
                or any(not _typed(value, contract["dataset"]["fields"][field]) for field, value in zip(fields, key))):
            raise ValueError("Invalid model state key")
        for value in key:
            _scalar(value, "model key")

    if not isinstance(state["cursors"], dict) or set(state["cursors"]) - identities.keys():
        raise ValueError("Unknown model cursor")
    for identity, cursor in state["cursors"].items():
        service = identities[identity]
        _object(cursor, {"access_path", "session", "position", "inclusive", "current_key"}, set(), "cursor")
        if cursor["access_path"] != service["access_path"] or cursor["session"] != service["session"] or type(cursor["inclusive"]) is not bool:
            raise ValueError("Model cursor identity mismatch")
        if cursor["position"] is not None:
            key_shape(cursor["position"], paths[service["access_path"]]["key_fields"])
        if cursor["current_key"] is not None:
            key_shape(cursor["current_key"], contract["dataset"]["primary_key"])
            if tuple(cursor["current_key"]) not in committed or cursor["position"] is None:
                raise ValueError("Unknown current model record")
            record = committed[tuple(cursor["current_key"])]
            if cursor["inclusive"] or cursor["position"] != [record[field] for field in paths[service["access_path"]]["key_fields"]]:
                raise ValueError("Current record does not match cursor position")
    seen_locks = set()
    for lock in _rows(state["locks"], "owned locks", MAX_RECORDS * len(identities)):
        _object(lock, {"key", "owner"}, set(), "owned lock")
        key_shape(lock["key"], contract["dataset"]["primary_key"])
        if lock["owner"] not in identities or tuple(lock["key"]) not in committed:
            raise ValueError("Unknown model lock identity")
        identity = tuple(lock["key"])
        if identity in seen_locks:
            raise ValueError("Exclusive model lock has multiple owners or duplicates")
        if contract["locking"]["read"] != "exclusive":
            raise ValueError("Owned model lock has no declared acquisition policy")
        seen_locks.add(identity)
    if not isinstance(state["committed_progress"], dict) or set(state["committed_progress"]) - identities.keys():
        raise ValueError("Unknown model progress cursor")
    for identity, key in state["committed_progress"].items():
        fields = paths[identities[identity]["access_path"]]["key_fields"]
        key_shape(key, fields)
        if not any([record[field] for field in fields] == key for record in committed.values()):
            raise ValueError("Committed cursor progress has no matching record")
    counters = {json.dumps([service["program"], operation["value"]], separators=(",", ":"))
                for service in contract["services"] for operation in service["operations"]}
    counts = state["call_counts"]
    if (not isinstance(counts, dict) or set(counts) - counters
            or any(type(value) is not int or not 1 <= value <= MAX_EVENTS for value in counts.values())
            or sum(counts.values()) > state["event_count"]):
        raise ValueError("Invalid model call counters")


def _effective_records(contract, state):
    records = {_primary(contract, record): record for record in state["committed_records"]}
    records.update({_primary(contract, record): record for record in state["pending_writes"]})
    return list(records.values())


def _view(contract, state, path):
    return sorted((record for record in _effective_records(contract, state)
                   if all(record[field] == value for field, value in path["select"].items())),
                  key=lambda record: tuple(record[field] for field in path["key_fields"]))


def _release(state, cursor_id):
    state["locks"] = [lock for lock in state["locks"] if lock["owner"] != cursor_id]


def _checkpoint_reason(contract, state, path, checkpoint):
    if checkpoint["schema_version"] != "1.0" or checkpoint["contract_version"] != contract["contract_version"] or checkpoint["contract_hash"] != state["contract_hash"]:
        return "restart_contract_mismatch"
    if checkpoint["dataset_hash"] != _hash(state["committed_records"]):
        return "restart_dataset_mismatch"
    if checkpoint["access_path"] != path["id"]:
        return "restart_access_path_mismatch"
    if (len(checkpoint["key"]) != len(path["key_fields"])
            or any(not _typed(value, contract["dataset"]["fields"][field]) for field, value in zip(path["key_fields"], checkpoint["key"]))):
        return "restart_key_type_mismatch"
    if not any([record[field] for field in path["key_fields"]] == checkpoint["key"] for record in state["committed_records"]):
        return "restart_committed_key_not_found"
    return None


@_input_errors
def apply_io_call(contract: dict, state: dict, program: str, values: dict, *, call_failure=False) -> list[dict]:
    """Apply one declared call without mutating inputs or performing real I/O.

    ``values`` are logical scalar source values, with storage padding already
    handled by the caller. Missing or non-scalar inputs stop at a boundary.
    Returned values are field updates, not a complete program environment.
    """
    c = validate_runtime_contract(contract)
    _text(program, "called program", True)
    if type(call_failure) is not bool:
        raise ValueError("call_failure must be boolean")
    if not isinstance(state, dict):
        raise ValueError("Model state must be an object")
    s = deepcopy(state)
    valid_state = False
    event = {"program": program.upper(), "runtime_verified": False,
             "semantics": "declared_contract", "function_value": None}
    service = next((item for item in c["services"] if item["program"] == program.upper()), None)

    def finish(status=None, *, updates=None, boundary=None, outcome="normal", detail=None):
        result_values = dict(updates or {})
        if status is not None and service is not None:
            result_values[service["status_field"]] = status
            event["status"] = status
        if detail is not None:
            event["detail"] = detail
        result = {"values": result_values, "state": _seal(s) if valid_state else s, "event": event, "outcome": outcome,
                  "runtime_verified": False}
        if boundary:
            result["boundary"] = {"reason": boundary, "runtime_verified": False}
            event["boundary"] = boundary
        return [result]

    if s.get("contract_hash") != _hash(c) or s.get("runtime_verified") is not False:
        return finish(boundary="io_state_contract_mismatch")
    if s.get("state_hash") != _hash({key: value for key, value in s.items() if key != "state_hash"}):
        return finish(boundary="io_state_integrity_mismatch")
    try:
        _state_shape(c, s)
    except (ValueError, TypeError, KeyError):
        return finish(boundary="io_state_shape_invalid")
    valid_state = True
    if type(s.get("event_count")) is not int or s["event_count"] >= MAX_EVENTS:
        return finish(boundary="io_event_budget_exhausted")
    s["event_count"] += 1
    if service is None:
        return finish(boundary="io_service_not_declared")
    if not isinstance(values, dict):
        return finish(boundary="io_values_not_scalar_environment")
    function = values.get(service["function_field"])
    event["function_value"] = function if type(function) in (int, str) else None
    if type(function) is not str:
        return finish(boundary="io_function_value_not_resolved")
    operation = next((op for op in service["operations"] if op["value"] == function), None)
    if operation is None:
        return finish(boundary="io_function_value_not_declared")
    action = operation["action"]
    event["action"] = action
    path = next(path for path in c["access_paths"] if path["id"] == service["access_path"])
    cursor_id = _cursor_id(service)
    event.update({"access_path": path["id"], "session": service["session"], "cursor_id": cursor_id})
    counter = json.dumps([service["program"], function], separators=(",", ":"))
    occurrence = s["call_counts"].get(counter, 0) + 1
    s["call_counts"][counter] = occurrence
    event["occurrence"] = occurrence
    fault = next((fault for fault in s["faults"] if fault["program"] == service["program"]
                  and fault["function"] == function and fault["occurrence"] == occurrence), None)
    if call_failure or (fault and fault["outcome"] == "exception"):
        return finish(outcome="exception", detail="scenario_call_failure_before_effects")
    if fault:
        return finish(fault["status"], detail="scenario_status_failure_before_effects")
    if path["join"]:
        return finish(boundary="io_join_access_path_not_modeled")
    statuses = c["statuses"]
    cursor = s["cursors"].get(cursor_id)

    def source_fields(mapping):
        result = {}
        for field, source in mapping.items():
            value = values.get(source)
            if not _typed(value, c["dataset"]["fields"][field]):
                return None
            if isinstance(value, str) and len(value) > 2048:
                return None
            if type(value) is int and abs(value) > 10**18 - 1:
                return None
            result[field] = value
        return result

    def new_cursor(position=None, inclusive=False):
        return {"access_path": path["id"], "session": service["session"], "position": position,
                "inclusive": inclusive, "current_key": None}

    def read_record(record, proposed_cursor, *, missing="eof"):
        if record is None:
            if c["locking"]["release_on_advance"]:
                _release(s, cursor_id)
            proposed_cursor["current_key"] = None
            s["cursors"][cursor_id] = proposed_cursor
            return finish(statuses[missing], detail="no_record_in_declared_access_path")
        key = _primary(c, record)
        if c["locking"]["read"] == "exclusive":
            held = any(tuple(lock["key"]) == key for lock in s["external_locks"])
            held = held or any(tuple(lock["key"]) == key and lock["owner"] != cursor_id for lock in s["locks"])
            if held:
                return finish(statuses["locked"], detail="declared_lock_conflict_cursor_preserved")
        if c["locking"]["release_on_advance"]:
            _release(s, cursor_id)
        if c["locking"]["read"] == "exclusive" and not any(tuple(lock["key"]) == key and lock["owner"] == cursor_id for lock in s["locks"]):
            s["locks"].append({"key": list(key), "owner": cursor_id})
        proposed_cursor.update({"position": [record[field] for field in path["key_fields"]],
                                "inclusive": False, "current_key": list(key)})
        s["cursors"][cursor_id] = proposed_cursor
        event["record_key"] = list(key)
        return finish(statuses["ok"], updates={source: record[field] for field, source in service["record_fields"].items()})

    def commit():
        s["committed_records"] = sorted(_effective_records(c, s), key=lambda record: _primary(c, record))
        event["committed_write_count"] = len(s["pending_writes"])
        s["pending_writes"] = []
        for identity, current in s["cursors"].items():
            if current["current_key"] is not None:
                s["committed_progress"][identity] = deepcopy(current["position"])
        if c["transaction"]["commit_cursor"] == "close":
            s["cursors"] = {}
        if c["transaction"]["commit_locks"] == "release":
            s["locks"] = []

    if action in {"first", "get"}:
        request = source_fields(service["key_fields"])
        if request is None:
            return finish(boundary="io_request_key_not_resolved")
        key = tuple(request[field] for field in path["key_fields"])
        proposed = new_cursor(list(key), True)
        if action == "first" and not operation["reads_record"]:
            if c["locking"]["release_on_advance"]:
                _release(s, cursor_id)
            s["cursors"][cursor_id] = proposed
            return finish(statuses["ok"], detail="position_only_next_includes_start_key")
        rows = _view(c, s, path)
        record = next((record for record in rows if (tuple(record[field] for field in path["key_fields"]) == key
                                                     if action == "get" else tuple(record[field] for field in path["key_fields"]) >= key)), None)
        return read_record(record, proposed, missing="not_found" if action == "get" else "eof")
    if action == "next":
        if cursor is None:
            return finish(statuses["invalid"], detail="cursor_not_positioned")
        position = cursor["position"]
        if position is None:
            return finish(statuses["invalid"], detail="cursor_not_positioned")
        record = next((record for record in _view(c, s, path)
                       if (tuple(record[field] for field in path["key_fields"]) >= tuple(position)
                           if cursor["inclusive"] else tuple(record[field] for field in path["key_fields"]) > tuple(position))), None)
        return read_record(record, deepcopy(cursor))
    if action == "save":
        if cursor is None or cursor["current_key"] is None:
            return finish(statuses["invalid"], detail="save_requires_current_record")
        record = source_fields(service["record_fields"])
        if record is None:
            return finish(boundary="io_write_record_not_resolved")
        key = _primary(c, record)
        old = next((item for item in _effective_records(c, s) if _primary(c, item) == tuple(cursor["current_key"])), None)
        if old is None or key != tuple(cursor["current_key"]):
            return finish(statuses["invalid"], detail="record_identity_change_not_supported")
        # Stable traversal requires every declared ordering field to be immutable.
        if any(record[field] != old[field] for access in c["access_paths"] for field in access["key_fields"]):
            return finish(statuses["invalid"], detail="access_path_key_change_not_supported")
        if any(tuple(lock["key"]) == key for lock in s["external_locks"]) or any(tuple(lock["key"]) == key and lock["owner"] != cursor_id for lock in s["locks"]):
            return finish(statuses["locked"], detail="declared_write_lock_conflict")
        if c["locking"]["save_requires_lock"] and not any(tuple(lock["key"]) == key and lock["owner"] == cursor_id for lock in s["locks"]):
            return finish(statuses["locked"], detail="save_requires_owned_read_lock")
        s["pending_writes"] = [item for item in s["pending_writes"] if _primary(c, item) != key] + [record]
        event["record_key"] = list(key)
        event["write_visibility"] = "pending"
        if c["transaction"]["mode"] == "autocommit":
            commit()
            event["write_visibility"] = "committed"
        return finish(statuses["ok"])
    if action == "close":
        s["cursors"].pop(cursor_id, None)
        if c["locking"]["release_on_close"]:
            _release(s, cursor_id)
        return finish(statuses["ok"], detail="cursor_closed_pending_transaction_unchanged")
    if action == "commit":
        commit()
        return finish(statuses["ok"])
    if action == "rollback":
        event["discarded_write_count"] = len(s["pending_writes"])
        s["pending_writes"] = []
        s["cursors"] = {}
        s["locks"] = []
        return finish(statuses["ok"], detail="pending_writes_discarded_committed_data_preserved")
    if action in {"checkpoint", "resume"} and not c["restart"]["enabled"]:
        return finish(boundary="io_restart_not_enabled")
    if action == "checkpoint":
        if s["pending_writes"]:
            return finish(boundary="restart_checkpoint_has_uncommitted_writes")
        progress = s["committed_progress"].get(cursor_id)
        if progress is None:
            return finish(boundary="restart_checkpoint_has_no_committed_progress")
        s["restart_checkpoint"] = {"schema_version": "1.0", "contract_hash": s["contract_hash"],
                                   "contract_version": c["contract_version"], "dataset_hash": _hash(s["committed_records"]),
                                   "access_path": path["id"], "key": deepcopy(progress)}
        event["checkpoint"] = deepcopy(s["restart_checkpoint"])
        return finish(statuses["ok"], detail="checkpoint_bound_to_committed_cursor_progress")
    if action == "resume":
        if s["pending_writes"]:
            return finish(boundary="restart_resume_has_uncommitted_writes")
        checkpoint = s["restart_checkpoint"]
        if checkpoint is None:
            return finish(boundary="restart_checkpoint_missing")
        reason = _checkpoint_reason(c, s, path, checkpoint)
        if reason:
            return finish(boundary=reason)
        key = tuple(checkpoint["key"])
        proposed = new_cursor(list(key), False)
        record = next((record for record in _view(c, s, path)
                       if tuple(record[field] for field in path["key_fields"]) > key), None)
        return read_record(record, proposed)
    return finish(boundary="io_action_not_modeled")
