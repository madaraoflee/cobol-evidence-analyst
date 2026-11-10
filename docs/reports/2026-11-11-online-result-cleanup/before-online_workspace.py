"""Isolated online workspaces, durable conversations and cancellable jobs."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from api_error_details import sanitize_diagnostic
from response_language import SUPPORTED_LANGUAGES
from online_material_format import (MaterialError, canonical_json, digest, relative_path,
                                    valid_commit, valid_hash)
from online_material_query import MaterialQuery
from online_material_reader import HTTPSObjectStore, MaterialReader
from online_source_client import PinnedSourceClient, _authorization
from online_state import OnlineState
from online_inventory_store import OnlineInventoryStore
from repository_scope import repository_inventory_requested


_ID = re.compile(r"[a-f0-9]{32}\Z")
_ERROR = re.compile(r"[A-Z][A-Z0-9_]{1,80}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}/[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_CREDENTIAL_KEYS = {"material_token", "material_username", "source_token", "source_username"}
_MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
_MAX_QUESTION = 16000
_MAX_CONVERSATIONS = 200
_MAX_WORKSPACES = 100
_MAX_JOBS = 200
_SOURCE_BASES = {"repository_snapshot", "sync_attested", "unknown"}


def _inventory_coverage(value):
    if not isinstance(value, dict) or value.get("active") is not True or value.get("scope") != "indexed_snapshot":
        return None
    result = {"scope": "indexed_snapshot", "active": True, "full_repository_or_runtime_proof": False}
    for key in ("inventory_complete", "complete", "legacy_checkpoint_recheck"):
        if type(value.get(key)) is not bool:
            return None
        result[key] = value[key]
    for key in ("files_discovered", "files_reviewed", "candidates_total", "candidates_reviewed", "pending_count"):
        if type(value.get(key)) is not int or value[key] < 0:
            return None
        result[key] = value[key]
    return result


def _inventory_answer_preview(answer, citations):
    """Display a bounded preview; the immutable publication retains the whole answer."""
    preview = answer[:4000].encode("utf-8")[:4000].decode("utf-8", errors="ignore")
    pattern = re.compile(r"\[(E[0-9a-f]{24})\]")
    while True:
        visible = set(pattern.findall(preview))
        selected = [item for item in citations if item.get("id") in visible]
        if len(canonical_json({"content": preview, "citations": selected})) <= 6000:
            break
        matches = list(pattern.finditer(preview))
        preview = preview[:matches[-1].start()] if matches else preview[:max(0, len(preview) // 2)]
    return preview, selected, preview != answer or len(selected) != len(citations)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MaterialError("ONLINE_INVALID_ID")
    return value


def _repository(value):
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise MaterialError("ONLINE_INVALID_REPOSITORY")
    return value


def _text(value, maximum, code, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise MaterialError(code)
    return value.strip()


def _credentials(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _CREDENTIAL_KEYS:
        raise MaterialError("ONLINE_INVALID_CREDENTIAL")
    result = {key: item for key, item in value.items() if item not in (None, "")}
    for prefix in ("material", "source"):
        _authorization(result.get(prefix + "_token"), result.get(prefix + "_username"), "ONLINE")
    return result


def _location(value):
    if not isinstance(value, dict):
        raise MaterialError("ONLINE_INVALID_LOCATION")
    if value.get("kind") == "https" and set(value) == {"kind", "base_url"}:
        HTTPSObjectStore(value["base_url"])
        return dict(value)
    if value.get("kind") == "downloads" and set(value) == {"kind", "workspace", "repository", "release_name"}:
        _repository(str(value["workspace"]) + "/" + str(value["repository"]))
        name = value["release_name"]
        if not isinstance(name, str) or not re.fullmatch(r"analysis-release-[a-f0-9]{64}\.json", name):
            raise MaterialError("ONLINE_INVALID_RELEASE")
        return dict(value)
    raise MaterialError("ONLINE_INVALID_LOCATION")


def _material_credential_scope(location):
    # A revision filename does not change a Downloads repository's authority.
    # An arbitrary HTTPS path may belong to another credential scope, even on
    # the same hostname, so no path-based credential expansion is inferred.
    if location["kind"] == "downloads":
        return ("downloads", location["workspace"], location["repository"])
    return ("https", location["base_url"])


def _guard_secrets(value, credentials):
    """Refuse accidental token exposure without rewriting signed evidence.

    Account names may legitimately be repository identities or source text.
    Replacing them would corrupt revision bindings. Tokens are never inserted
    into prompts; if one nevertheless appears, preserve the prior checkpoint
    and expose only a bounded diagnostic instead of storing a modified copy.
    """
    private = {item for key, item in credentials.items() if key.endswith("_token") and isinstance(item, str) and item}
    def visit(item):
        if isinstance(item, str):
            if any(secret in item for secret in private):
                raise MaterialError("ONLINE_CREDENTIAL_EXPOSURE")
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key)
                visit(child)
    visit(value)
    return value


def _safe_error(error):
    value = getattr(error, "code", None)
    return value if isinstance(value, str) and _ERROR.fullmatch(value) else "ONLINE_OPERATION_FAILED"


def _source_basis(release):
    """Summarize a validated release without equating an attestation to proof."""
    if not isinstance(release, dict):
        return "unknown"
    if release.get("schema_version") == "online-release/v1" and isinstance(release.get("sync_receipt"), dict):
        return "sync_attested" if release["sync_receipt"].get("complete") is True else "unknown"
    if (release.get("schema_version") == "online-release/v2"
            and isinstance(release.get("source_snapshot"), dict)
            and release["source_snapshot"].get("sync_completeness") == "not_verified"):
        return "repository_snapshot"
    return "unknown"


def _coverage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    if isinstance(value.get("scope"), str):
        result["scope"] = value["scope"][:128]
    for key in ("indexed_files", "source_bytes"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    boundaries = value.get("boundaries", [])
    if isinstance(boundaries, list):
        result["boundaries"] = deepcopy(boundaries[:64])
        result["boundary_count"] = len(boundaries)
        result["boundaries_truncated"] = len(boundaries) > 64
    scope = value.get("build_scope")
    complete = scope.get("input_coverage_complete") if isinstance(scope, dict) else None
    result["input_coverage_complete"] = complete if type(complete) is bool else None
    skips = value.get("input_skips", [])
    if isinstance(skips, list):
        details = [{key: item[key] for key in ("relative_path", "reason_code") if isinstance(item.get(key), str)}
                   for item in skips[:16] if isinstance(item, dict)]
        result.update(input_skip_count=len(skips), input_skips=details,
                      input_skips_truncated=len(details) < len(skips))
    else:
        result.update(input_skip_count=None, input_skips=[], input_skips_truncated=True)
    if len(canonical_json(result)) > 16384:
        result["boundaries"] = [{"code": "COVERAGE_DETAILS_REQUIRE_MATERIAL"}]
        result["boundaries_truncated"] = True
        result["input_skips"] = []
        result["input_skips_truncated"] = result["input_skip_count"] != 0
    return result


class OnlineWorkbench:
    """All state and credentials are separate from the local-source workbench.

    Injected factories are for tests or approved embedded transports. A material
    factory receives ``(location, credentials, expected, check_cancel)`` and
    returns a MaterialReader. A source factory receives ``(repository,
    credentials, check_cancel)`` and returns the pinned read-file callable.
    Neither injection is selectable through an HTTP payload.
    """

    def __init__(self, root: Path, *, config_provider=None, answer_instructions_provider=None, material_factory=None,
                 source_factory=None, investigator=None):
        self.lock = threading.RLock()
        self.store = OnlineState(root)
        self._inventory_store = None
        self.config_provider = config_provider
        self.answer_instructions_provider = answer_instructions_provider
        self.material_factory = material_factory
        self.source_factory = source_factory
        self.investigator = investigator
        self._credentials = {}
        self._events = {}
        self._threads = {}
        self._closed = False
        self._active_job_id = None
        try:
            self._recover()
        except BaseException:
            try:
                self.store.close()
            except sqlite3.Error:
                pass
            raise

    @property
    def inventory_store(self):
        with self.lock:
            if self._inventory_store is None:
                self._inventory_store = OnlineInventoryStore(self.store.root / "inventories")
            return self._inventory_store

    def _recover(self):
        with self.lock, self.store.transaction() as db:
            for job in self.store.all(db, "jobs"):
                if not isinstance(job, dict) or not _ID.fullmatch(str(job.get("job_id", ""))):
                    raise MaterialError("ONLINE_STATE_INVALID")
                if job.get("status") == "running":
                    job.update(status="interrupted", error="ONLINE_PROCESS_INTERRUPTED", updated_at=_now())
                    self.store.put(db, "jobs", job["job_id"], job)
                    if job.get("conversation_id"):
                        conversation = self.store.get(db, "conversations", job["conversation_id"])
                        if conversation is not None and conversation.get("status") == "running":
                            conversation["status"] = "interrupted"
                            if conversation.get("pending"):
                                conversation["pending"]["status"] = "interrupted"
                            self._save_conversation(db, conversation)

    def close(self):
        with self.lock:
            self._closed = True
            for event in self._events.values():
                event.set()
            self._credentials.clear()
            self._close_idle_stores()

    def _close_idle_stores(self):
        # A cancelled worker still persists its final interrupted/unknown
        # state. Close its stores after that cleanup, without joining a remote
        # request or allowing a late callback to leave a reopened pool behind.
        if self._threads:
            return
        try:
            if self._inventory_store is not None:
                self._inventory_store.close()
        finally:
            self.store.close()

    def _require_open(self):
        if self._closed:
            raise MaterialError("ONLINE_CLOSED")

    def _get(self, db, table, identifier):
        value = self.store.get(db, table, identifier)
        if value is None:
            raise MaterialError({"workspaces": "ONLINE_WORKSPACE_NOT_FOUND", "conversations": "ONLINE_CONVERSATION_NOT_FOUND",
                                 "jobs": "ONLINE_JOB_NOT_FOUND"}[table])
        return value

    def _workspace(self, db, identifier):
        valid_hash(identifier)
        value = self._get(db, "workspaces", identifier)
        self._validate_workspace(value)
        return value

    @staticmethod
    def _validate_workspace(workspace):
        try:
            identity = {"repository": workspace["repository"], "revision": workspace["revision"]}
            _repository(identity["repository"]["identity"])
            valid_commit(identity["repository"]["commit"])
            relative_path(identity["repository"]["subdirectory"], allow_empty=True)
            valid_hash(identity["revision"])
            if digest(canonical_json(identity)) != workspace["id"]:
                raise ValueError()
            _location(workspace["location"])
            if workspace.get("source_basis", "unknown") not in _SOURCE_BASES:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise MaterialError("ONLINE_STATE_INVALID") from None

    def _conversation(self, db, identifier):
        _identifier(identifier)
        value = self._get(db, "conversations", identifier)
        if value.get("id") != identifier or not isinstance(value.get("messages"), list):
            raise MaterialError("ONLINE_STATE_INVALID")
        self._workspace(db, value.get("workspace_id"))
        return value

    def _selection(self, db):
        value = self.store.get(db, "settings", "selection")
        return value or {"workspace_id": None, "conversation_id": None}

    def _select(self, db, workspace_id, conversation_id=None):
        self.store.put(db, "settings", "selection", {"workspace_id": workspace_id, "conversation_id": conversation_id})

    @staticmethod
    def _summary(conversation):
        return {key: conversation.get(key) for key in ("id", "workspace_id", "title", "created_at", "updated_at", "status")}

    def _save_conversation(self, db, conversation):
        self.store.put(db, "conversations", conversation["id"], conversation)
        self.store.put(db, "conversation_summaries", conversation["id"], self._summary(conversation))

    def state(self):
        with self.lock, self.store.transaction() as db:
            workspaces = self.store.all(db, "workspaces")
            for workspace in workspaces:
                self._validate_workspace(workspace)
                workspace.setdefault("source_basis", "unknown")
                workspace["connected"] = workspace["id"] in self._credentials
            conversations = self.store.all(db, "conversation_summaries")
            selection = self._selection(db)
            active = self.store.get(db, "jobs", self._active_job_id) if self._active_job_id else None
            conversation = (self._conversation(db, selection["conversation_id"])
                            if selection.get("conversation_id") else None)
            return {"workspaces": sorted(workspaces, key=lambda item: item["created_at"], reverse=True),
                    "conversations": sorted([self._summary(item) for item in conversations],
                                            key=lambda item: item["updated_at"], reverse=True),
                    "current_workspace_id": selection.get("workspace_id"),
                    "current_conversation_id": selection.get("conversation_id"),
                    "conversation": self._public_conversation(conversation), "active_job": active, "error": None}

    @staticmethod
    def _public_conversation(conversation):
        if conversation is None:
            return None
        result = deepcopy(conversation)
        pending = result.get("pending")
        if pending:
            checkpoint = pending.pop("checkpoint", None)
            pending["can_resume"] = pending.get("status") != "running"
            pending["retry_confirmation_required"] = (isinstance(checkpoint, dict)
                and checkpoint.get("request_status") in {"in_flight", "unknown"})
        return result

    def _reader(self, location, credentials, expected, check_cancel):
        if self.material_factory is not None:
            return self.material_factory(deepcopy(location), dict(credentials), dict(expected), check_cancel)
        release = None
        if location["kind"] == "https":
            store = HTTPSObjectStore(location["base_url"], token=credentials.get("material_token"),
                                     username=credentials.get("material_username"))
        else:
            from online_distribution import DownloadsClient, DownloadsObjectStore, load_release
            client = DownloadsClient(location["workspace"], location["repository"],
                                     token=credentials.get("material_token"), username=credentials.get("material_username"),
                                     check_cancel=check_cancel)
            release = load_release(client, location["release_name"])
            for key, value in (("repository", release["repository"]["identity"]),
                               ("commit", release["repository"]["commit"]), ("revision", release["revision"])):
                if expected.get(key) is not None and expected[key] != value:
                    raise MaterialError("ONLINE_RELEASE_MISMATCH")
                expected[key] = value
            store = DownloadsObjectStore(client, release["revision"], release=release)
        reader = MaterialReader(store, expected_repository=expected.get("repository"), expected_commit=expected.get("commit"),
                                expected_revision=expected.get("revision"), check_cancel=check_cancel)
        reader.source_basis = _source_basis(release)
        if hasattr(store, "reset_network_budget"):
            store.reset_network_budget()
        return reader

    def _source(self, repository, credentials, check_cancel):
        if self.source_factory is not None:
            return self.source_factory(deepcopy(repository), dict(credentials), check_cancel)
        workspace, slug = repository["identity"].split("/")
        client = PinnedSourceClient(workspace, slug, repository["commit"], token=credentials.get("source_token"),
                                    username=credentials.get("source_username"), check_cancel=check_cancel)
        prefix = repository["subdirectory"] + "/" if repository["subdirectory"] else ""
        def read(path, *, max_bytes):
            check_cancel()
            data = client.read_file(prefix + relative_path(path), max_bytes=max_bytes)
            check_cancel()
            return data
        return read

    def _session(self, workspace, credentials, check_cancel):
        reader = self._reader(workspace["location"], credentials,
                              {"repository": workspace["repository"]["identity"], "commit": workspace["repository"]["commit"],
                               "revision": workspace["revision"]}, check_cancel)
        if reader.manifest["repository"] != workspace["repository"]:
            raise MaterialError("ONLINE_RELEASE_MISMATCH")
        return MaterialQuery(reader), self._source(workspace["repository"], credentials, check_cancel)

    def _new_job(self, db, kind, *, workspace_id=None, conversation_id=None):
        self._require_open()
        if self._active_job_id:
            current = self.store.get(db, "jobs", self._active_job_id)
            if current and current["status"] == "running":
                raise MaterialError("ONLINE_BUSY")
        jobs = sorted(self.store.all(db, "jobs"), key=lambda value: value["created_at"])
        for job in jobs[:max(0, len(jobs) - _MAX_JOBS + 1)]:
            self.store.delete(db, "jobs", job["job_id"])
        identifier = secrets.token_hex(16)
        job = {"job_id": identifier, "kind": kind, "status": "running", "workspace_id": workspace_id,
               "conversation_id": conversation_id, "created_at": _now(), "updated_at": _now(),
               "progress": {"phase": "preparing"}, "error": None, "cancel_requested": False}
        self.store.put(db, "jobs", identifier, job)
        return job

    def _launch(self, job, work):
        event = threading.Event()
        self._active_job_id = job["job_id"]
        self._events[job["job_id"]] = event
        def check_cancel():
            if event.is_set() or self._closed:
                raise MaterialError("ONLINE_CANCELLED")
        def run():
            try:
                work(check_cancel)
            except Exception as error:
                self._fail_job(job["job_id"], _safe_error(error), getattr(error, "diagnostic", None))
            finally:
                with self.lock:
                    self._events.pop(job["job_id"], None)
                    self._threads.pop(job["job_id"], None)
                    if self._closed:
                        self._close_idle_stores()
        thread = threading.Thread(target=run, name="online-analysis-" + job["job_id"][:8], daemon=True)
        self._threads[job["job_id"]] = thread
        thread.start()

    def _fail_job(self, identifier, code, diagnostic=None):
        with self.lock, self.store.transaction() as db:
            job = self._get(db, "jobs", identifier)
            status = "cancelled" if code == "ONLINE_CANCELLED" else "failed"
            diagnostic = sanitize_diagnostic(diagnostic)
            job.update(status=status, error=code, diagnostic=diagnostic, updated_at=_now())
            self.store.put(db, "jobs", identifier, job)
            if job.get("conversation_id"):
                conversation = self._conversation(db, job["conversation_id"])
                conversation.update(status=status, updated_at=_now())
                if conversation.get("pending"):
                    conversation["pending"]["status"] = status
                    conversation["pending"]["error"] = code
                    conversation["pending"]["diagnostic"] = diagnostic
                self._save_conversation(db, conversation)

    def connect(self, payload, *, updating=False):
        location = _location(payload.get("location"))
        expected = {"repository": _repository(payload.get("repository")),
                    "commit": valid_commit(payload["commit"]) if payload.get("commit") else None,
                    "revision": valid_hash(payload["revision"]) if payload.get("revision") else None}
        credentials = _credentials(payload.get("credentials"))
        label = _text(payload.get("label", expected["repository"]), 200, "ONLINE_INVALID_LABEL")
        old_id = payload.get("workspace_id") if updating else None
        with self.lock:
            with self.store.transaction() as db:
                if updating:
                    old = self._workspace(db, old_id)
                    if old["repository"]["identity"] != expected["repository"]:
                        raise MaterialError("ONLINE_UPDATE_REPOSITORY_MISMATCH")
                    supplied = payload.get("credentials") or {}
                    previous_credentials = self._credentials.get(old_id, {})
                    for prefix in ("material", "source"):
                        keys = {prefix + "_token", prefix + "_username"}
                        if keys & supplied.keys():
                            # Supplying empty fields deliberately clears this
                            # credential group; never silently put it back.
                            continue
                        if (prefix == "material" and _material_credential_scope(old["location"]) != _material_credential_scope(location)
                                and keys & previous_credentials.keys()):
                            raise MaterialError("ONLINE_MATERIAL_REAUTH_REQUIRED")
                        credentials.update({key: value for key, value in previous_credentials.items() if key in keys})
                start_selection = self._selection(db)
                _guard_secrets({"location": location, "repository": expected["repository"], "label": label}, credentials)
                job = self._new_job(db, "update" if updating else "connect", workspace_id=old_id)
            def work(check_cancel):
                reader = self._reader(location, credentials, expected, check_cancel)
                manifest = reader.manifest
                _repository(manifest["repository"]["identity"])
                identity = {"repository": manifest["repository"], "revision": manifest["revision"]}
                identifier = digest(canonical_json(identity))
                workspace = {"id": identifier, **identity, "location": location, "label": label, "created_at": _now(),
                             "source_basis": getattr(reader, "source_basis", "unknown"),
                             "coverage": _coverage(manifest["coverage"]), "analysis": {
                                 key: manifest["analysis"].get(key) for key in ("index_kind", "parser_version", "commit_verification")}}
                self._validate_workspace(workspace)
                _guard_secrets(workspace, credentials)
                check_cancel()
                with self.lock, self.store.transaction() as db:
                    check_cancel()
                    previous = self.store.get(db, "workspaces", identifier)
                    if previous is None and len(self.store.all(db, "workspaces")) >= _MAX_WORKSPACES:
                        raise MaterialError("ONLINE_WORKSPACE_LIMIT")
                    if updating and manifest["repository"]["subdirectory"] != old["repository"]["subdirectory"]:
                        raise MaterialError("ONLINE_UPDATE_SCOPE_MISMATCH")
                    if previous:
                        workspace["created_at"] = previous["created_at"]
                    self.store.put(db, "workspaces", identifier, workspace)
                    # Reconnecting an unchanged revision does not detach its open conversation.
                    selected = self._selection(db)
                    adopted = selected == start_selection
                    if adopted:
                        self._select(db, identifier, selected.get("conversation_id") if selected.get("workspace_id") == identifier else None)
                    completed = self._get(db, "jobs", job["job_id"])
                    completed.update(status="completed", workspace_id=identifier, updated_at=_now(),
                                     progress={"phase": "ready"}, unchanged=(old_id == identifier), adopted=adopted)
                    self.store.put(db, "jobs", job["job_id"], completed)
                    self._credentials[identifier] = dict(credentials)
            self._launch(job, work)
        return {"job_id": job["job_id"], "job": job}

    def open(self, payload):
        with self.lock, self.store.transaction() as db:
            if payload.get("conversation_id"):
                conversation = self._conversation(db, payload["conversation_id"])
                self._select(db, conversation["workspace_id"], conversation["id"])
            else:
                workspace = self._workspace(db, payload.get("workspace_id"))
                self._select(db, workspace["id"])
        return self.state()

    def credentials(self, payload):
        credentials = _credentials(payload.get("credentials"))
        with self.lock, self.store.transaction() as db:
            workspace = self._workspace(db, payload.get("workspace_id"))
            self._credentials[workspace["id"]] = credentials
        return {"workspace_id": workspace["id"], "connected": True}

    def disconnect(self, payload):
        with self.lock, self.store.transaction() as db:
            workspace = self._workspace(db, payload.get("workspace_id"))
            self._credentials.pop(workspace["id"], None)
            if self._active_job_id:
                job = self.store.get(db, "jobs", self._active_job_id)
                if job and job.get("workspace_id") == workspace["id"] and job["status"] == "running":
                    self._events[self._active_job_id].set()
        return {"workspace_id": workspace["id"], "connected": False}

    def new_conversation(self, payload):
        with self.lock, self.store.transaction() as db:
            workspace = self._workspace(db, payload.get("workspace_id"))
            if len(self.store.all(db, "conversation_summaries")) >= _MAX_CONVERSATIONS:
                raise MaterialError("ONLINE_HISTORY_LIMIT")
            identifier = secrets.token_hex(16)
            conversation = {"id": identifier, "workspace_id": workspace["id"], "title": "新的在线对话", "created_at": _now(),
                            "updated_at": _now(), "status": "idle", "messages": [], "pending": None}
            self._save_conversation(db, conversation)
            self._select(db, workspace["id"], identifier)
        return {"conversation": self._public_conversation(conversation)}

    def question(self, payload, *, resume=False):
        if payload.get("allow_source_transfer") is not True:
            raise MaterialError("ONLINE_SOURCE_TRANSFER_REQUIRED")
        approve_retry = payload.get("approve_retry", False)
        if type(approve_retry) is not bool:
            raise MaterialError("ONLINE_INVALID_RETRY_APPROVAL")
        instructions = self.answer_instructions_provider() if self.answer_instructions_provider is not None else ""
        if not isinstance(instructions, str):
            raise MaterialError("ONLINE_INVALID_ANSWER_REQUIREMENTS")
        locale = payload.get("locale")
        if locale is not None and (not isinstance(locale, str) or locale not in SUPPORTED_LANGUAGES):
            raise MaterialError("ONLINE_INVALID_LOCALE")
        with self.lock:
            with self.store.transaction() as db:
                conversation = self._conversation(db, payload.get("conversation_id"))
                workspace = self._workspace(db, conversation["workspace_id"])
                if workspace["id"] not in self._credentials:
                    raise MaterialError("ONLINE_RECONNECT_REQUIRED")
                credentials = dict(self._credentials[workspace["id"]])
                _guard_secrets(instructions, credentials)
                if resume:
                    pending = conversation.get("pending")
                    if not pending or pending.get("status") == "running":
                        raise MaterialError("ONLINE_NOTHING_TO_RESUME")
                    question = pending["question"]
                    # Before the first model boundary, connection/configuration
                    # failures have no checkpoint and are safe to retry. Once
                    # a request starts, the runner has persisted its status.
                    checkpoint = deepcopy(pending.get("checkpoint"))
                    if locale is None:
                        locale = pending.get("locale")
                else:
                    if conversation.get("pending"):
                        raise MaterialError("ONLINE_PENDING_INVESTIGATION")
                    question = _text(payload.get("question"), _MAX_QUESTION, "ONLINE_INVALID_QUESTION")
                    _guard_secrets(question, credentials)
                    checkpoint = None
                    user_id = secrets.token_hex(16)
                    conversation["messages"].append({"id": user_id, "role": "user", "content": question,
                                                      "created_at": _now(), "status": "pending"})
                    conversation["pending"] = {"question": question, "user_message_id": user_id,
                                                 "checkpoint": None, "status": "running"}
                    if len(conversation["messages"]) == 1:
                        conversation["title"] = question[:120]
                job = self._new_job(db, "resume" if resume else "question", workspace_id=workspace["id"], conversation_id=conversation["id"])
                conversation.update(status="running", updated_at=_now())
                conversation["pending"]["status"] = "running"
                conversation["pending"]["locale"] = locale
                self._save_conversation(db, conversation)
                self._select(db, workspace["id"], conversation["id"])
            def work(check_cancel):
                query, source = self._session(workspace, credentials, check_cancel)
                source_stats = {"source_bytes": 0, "source_requests": 0}
                def read_source(path, *, max_bytes):
                    check_cancel()
                    data = source(path, max_bytes=max_bytes)
                    source_stats["source_requests"] += 1
                    source_stats["source_bytes"] += len(data)
                    check_cancel()
                    return data
                def progress(event):
                    self._progress(job["job_id"], event, credentials)
                runner = self.investigator
                if runner is None:
                    from online_investigation import run_investigation
                    runner = run_investigation
                config = self.config_provider() if self.config_provider is not None else None
                history = []
                for message in conversation["messages"]:
                    if message["id"] == conversation["pending"]["user_message_id"]:
                        break
                    item = {"role": message["role"], "content": message["content"]}
                    citations = message.get("citations", [])
                    if message.get("inventory_answer_preview") and message.get("inventory_result"):
                        publication = self.inventory_store.publication(message["inventory_result"]["id"],
                            conversation_id=conversation["id"], workspace_id=workspace["id"], revision=workspace["revision"])
                        item["content"] = publication.data["answer"]
                        citations = publication.data.get("answer_citations", [])
                    if message["role"] == "assistant" and citations:
                        item["citations"] = [{key: deepcopy(citation.get(key))
                                              for key in ("path", "start", "end", "commit", "revision", "hash")}
                                             for citation in citations[-64:]]
                        item["citations_omitted"] = max(0, len(citations) - 64)
                    history.append(item)
                inventory_options = {}
                if repository_inventory_requested(question):
                    session_id = checkpoint.get("inventory_session", {}).get("id") if isinstance(checkpoint, dict) else None
                    inventory_options["inventory_session"] = self.inventory_store.session(
                        workspace_id=workspace["id"], conversation_id=conversation["id"],
                        user_message_id=conversation["pending"]["user_message_id"], manifest=query.reader.manifest,
                        question=question, identifier=session_id, guard=lambda value: _guard_secrets(value, credentials))
                with self.store.analysis_writes():
                    result = runner(query, read_source, question, config=config, history=history, checkpoint=checkpoint,
                                    allow_source_transfer=True, check_cancel=check_cancel, progress=progress, approve_retry=approve_retry,
                                    answer_instructions=instructions, locale=locale, **inventory_options)
                result = _guard_secrets(result, credentials)
                self._complete(job["job_id"], result, {"material_bytes": query.reader.fetched_bytes,
                               "network_material_bytes": getattr(query.reader, "network_fetched_bytes", query.reader.fetched_bytes),
                               "material_objects": query.reader.fetched_objects, **source_stats})
            self._launch(job, work)
        return {"job_id": job["job_id"], "job": job}

    def _progress(self, identifier, event, credentials):
        if not isinstance(event, dict):
            return
        event = deepcopy(event)
        checkpoint = event.get("checkpoint")
        if checkpoint is not None and (not isinstance(checkpoint, dict) or len(canonical_json(checkpoint)) > _MAX_CHECKPOINT_BYTES):
            raise MaterialError("ONLINE_CHECKPOINT_LIMIT")
        if isinstance(checkpoint, dict) and "diagnostic" in checkpoint:
            checkpoint["diagnostic"] = sanitize_diagnostic(checkpoint["diagnostic"])
        safe = _guard_secrets(event, credentials)
        visible = {key: safe[key] for key in ("phase", "status", "model_calls", "actions", "completed", "total")
                   if key in safe and isinstance(safe[key], (str, int, float, bool))}
        if len(canonical_json(visible)) > 8192:
            visible = {"phase": "investigating"}
        with self.lock, self.store.transaction() as db:
            job = self._get(db, "jobs", identifier)
            job.update(progress=visible, updated_at=_now())
            coverage_ledger = _inventory_coverage(safe.get("coverage_ledger"))
            if coverage_ledger is not None:
                job["coverage_ledger"] = coverage_ledger
            if isinstance(checkpoint, dict):
                job["diagnostic"] = sanitize_diagnostic(checkpoint.get("diagnostic"))
            self.store.put(db, "jobs", identifier, job)
            if checkpoint is not None:
                conversation = self._conversation(db, job["conversation_id"])
                if conversation.get("pending"):
                    conversation["pending"]["checkpoint"] = safe["checkpoint"]
                    conversation["pending"]["diagnostic"] = sanitize_diagnostic(checkpoint.get("diagnostic"))
                    if coverage_ledger is not None:
                        conversation["pending"]["coverage_ledger"] = coverage_ledger
                    conversation["updated_at"] = _now()
                    self._save_conversation(db, conversation)

    @staticmethod
    def _retained_recovery_answer(conversation, pending, status, answer, citations, evidence, coverage):
        if status not in {"failed", "cancelled", "retry_confirmation_required"}:
            return False
        previous = None
        for message in reversed(conversation["messages"]):
            if message.get("role") == "user":
                if message.get("id") != pending["user_message_id"]:
                    return False
                break
            if previous is None and message.get("role") == "assistant":
                previous = message
        else:
            return False
        if previous is None:
            return False
        old_citations = previous.get("citations", [])
        if not isinstance(old_citations, list) or any(not isinstance(item, dict) for item in old_citations):
            return False
        # UI keys identify a delivered message; the source citation identity
        # and all answer evidence still have to match exactly.
        without_keys = lambda items: [{key: value for key, value in item.items() if key != "key"}
                                      for item in items]
        return (previous.get("content") == answer and without_keys(old_citations) == without_keys(citations)
                and previous.get("evidence", []) == evidence and previous.get("coverage", {}) == coverage)

    def _complete(self, identifier, result, transfer):
        statuses = {"answered", "needs_continuation", "cancelled", "failed", "retry_confirmation_required"}
        if not isinstance(result, dict) or result.get("status") not in statuses:
            raise MaterialError("ONLINE_INVALID_RESULT")
        checkpoint = deepcopy(result.get("checkpoint"))
        if checkpoint is not None and (not isinstance(checkpoint, dict) or len(canonical_json(checkpoint)) > _MAX_CHECKPOINT_BYTES):
            raise MaterialError("ONLINE_CHECKPOINT_LIMIT")
        if isinstance(checkpoint, dict) and "diagnostic" in checkpoint:
            checkpoint["diagnostic"] = sanitize_diagnostic(checkpoint["diagnostic"])
        answer = _text(result.get("answer", ""), 1024 * 1024, "ONLINE_INVALID_ANSWER", empty=True)
        with self.lock, self.store.transaction() as db:
            job = self._get(db, "jobs", identifier)
            conversation = self._conversation(db, job["conversation_id"])
            status = result["status"]
            error = result.get("error")
            error = error if isinstance(error, str) and _ERROR.fullmatch(error) else None
            diagnostic = sanitize_diagnostic(result.get("diagnostic"))
            coverage_ledger = _inventory_coverage(result.get("coverage_ledger"))
            job.update(status=status, error=error, diagnostic=diagnostic, updated_at=_now(), transfer=transfer,
                       progress={"phase": status, "model_calls": result.get("model_calls", 0), "actions": result.get("actions", 0)})
            if coverage_ledger is not None:
                job["coverage_ledger"] = coverage_ledger
            conversation.update(status=status, updated_at=_now())
            pending = conversation.get("pending")
            if pending is None:
                raise MaterialError("ONLINE_STATE_INVALID")
            if answer or result.get("citations"):
                citations = deepcopy(result.get("citations") or [])
                if (not isinstance(citations, list) or len(citations) > 1000
                        or any(not isinstance(citation, dict) for citation in citations)):
                    raise MaterialError("ONLINE_INVALID_RESULT")
                evidence = deepcopy(result.get("evidence") or [])
                coverage = deepcopy(result.get("coverage") or {})
                preview = False
                if result.get("inventory_result"):
                    # Full original-source locations and narrative live in the
                    # immutable result. Repeating raw windows here would fill
                    # ordinary conversation storage during a long investigation.
                    evidence = []
                    answer, citations, preview = _inventory_answer_preview(answer, citations)
                retained = self._retained_recovery_answer(conversation, pending, status, answer,
                                                          citations, evidence, coverage)
                if not retained:
                    message_id = secrets.token_hex(16)
                    for index, citation in enumerate(citations):
                        citation["key"] = message_id + ":" + str(index)
                    message = {"id": message_id, "role": "assistant", "content": answer, "created_at": _now(),
                               "status": status, "citations": citations, "evidence": evidence,
                               "coverage": coverage, "job_id": identifier}
                    if coverage_ledger is not None:
                        message["coverage_ledger"] = coverage_ledger
                        if result.get("inventory_result"):
                            publication = self.inventory_store.publication(result["inventory_result"].get("id"),
                                conversation_id=conversation["id"], workspace_id=conversation["workspace_id"],
                                revision=checkpoint["revision"])
                            message["inventory_result"] = publication.page(preview=False)
                            message["inventory_answer_preview"] = preview
                        else:
                            message["findings"] = deepcopy(result.get("findings", []))
                    conversation["messages"].append(message)
            for message in conversation["messages"]:
                if message["id"] == pending["user_message_id"]:
                    message["status"] = status
                    break
            if status == "answered":
                conversation["pending"] = None
            else:
                pending.update(checkpoint=checkpoint, status=status, error=error, diagnostic=diagnostic)
                if coverage_ledger is not None:
                    pending["coverage_ledger"] = coverage_ledger
            self._save_conversation(db, conversation)
            self.store.put(db, "jobs", identifier, job)

    def cancel(self, payload):
        identifier = _identifier(payload.get("job_id"))
        with self.lock, self.store.transaction() as db:
            job = self._get(db, "jobs", identifier)
            if job["status"] == "running":
                event = self._events.get(identifier)
                if event:
                    event.set()
                job["cancel_requested"] = True
                self.store.put(db, "jobs", identifier, job)
        return {"job": job}

    def delete_conversation(self, identifier):
        with self.lock, self.store.transaction() as db:
            conversation = self._conversation(db, identifier)
            if conversation["status"] == "running":
                raise MaterialError("ONLINE_BUSY")
            self.store.delete(db, "conversations", identifier)
            self.store.delete(db, "conversation_summaries", identifier)
            selection = self._selection(db)
            if selection.get("conversation_id") == identifier:
                self._select(db, selection.get("workspace_id"))
        if (self.store.root / "inventories").exists():
            self.inventory_store.delete_conversation(identifier)
        return {"deleted": identifier}

    def export(self, query):
        with self.lock, self.store.transaction() as db:
            conversation = self._conversation(db, query.get("conversation_id"))
            workspace = self._workspace(db, conversation["workspace_id"])
        public = self._public_conversation(conversation)
        public.pop("pending", None)
        inventories = {}
        for message in public["messages"]:
            descriptor = message.get("inventory_result")
            if descriptor and descriptor["id"] not in inventories:
                inventories[descriptor["id"]] = self.inventory_store.publication(descriptor["id"],
                    conversation_id=conversation["id"], workspace_id=workspace["id"], revision=workspace["revision"]).export_data()
        data = {"schema_version": "online-conversation-export/v1", "workspace": workspace, "conversation": public}
        if inventories:
            data["inventories"] = inventories
        if query.get("format", "markdown") == "json":
            content = json.dumps(data, ensure_ascii=False, indent=2)
            return {"filename": "online-conversation-" + conversation["id"] + ".json", "mime_type": "application/json", "content": content}
        if query.get("format", "markdown") != "markdown":
            raise MaterialError("ONLINE_INVALID_EXPORT_FORMAT")
        lines = ["# " + conversation["title"], "", "Repository: " + workspace["repository"]["identity"],
                 "Commit: " + workspace["repository"]["commit"], "Revision: " + workspace["revision"], ""]
        for message in conversation["messages"]:
            lines.extend(["## " + ("Question" if message["role"] == "user" else "Answer"), "", message["content"], ""])
            if message.get("citations"):
                lines.extend(["Citations:", "", "```json", json.dumps(message["citations"], ensure_ascii=False, indent=2), "```", ""])
            if message.get("coverage"):
                lines.extend(["Coverage:", "", "```json", json.dumps(message["coverage"], ensure_ascii=False, indent=2), "```", ""])
        for identifier, inventory in inventories.items():
            lines.extend(["## " + identifier, "", inventory["answer"], "", "```json",
                          json.dumps(inventory, ensure_ascii=False, indent=2), "```", ""])
        return {"filename": "online-conversation-" + conversation["id"] + ".md", "mime_type": "text/markdown", "content": "\n".join(lines)}

    def evidence(self, query):
        citation_id = _text(query.get("citation_id"), 128, "ONLINE_INVALID_CITATION")
        with self.lock, self.store.transaction() as db:
            conversation = self._conversation(db, query.get("conversation_id"))
            workspace = self._workspace(db, conversation["workspace_id"])
            if workspace["id"] not in self._credentials:
                raise MaterialError("ONLINE_RECONNECT_REQUIRED")
            credentials = dict(self._credentials[workspace["id"]])
            matches = [citation for message in conversation["messages"] for citation in message.get("citations", [])
                       if citation_id in (citation.get("key"), citation.get("id"))]
        unique = {canonical_json({key: value for key, value in item.items() if key != "key"}): item for item in matches}
        if len(unique) != 1:
            raise MaterialError("ONLINE_CITATION_NOT_FOUND")
        citation = next(iter(unique.values()))
        if citation.get("revision") != workspace["revision"] or citation.get("commit") != workspace["repository"]["commit"]:
            raise MaterialError("ONLINE_CITATION_MISMATCH")
        service, source = self._session(workspace, credentials, self._require_open)
        result = service.evidence(citation.get("path"), citation.get("start"), citation.get("end"), source)
        if result["citation"]["hash"] != citation.get("hash"):
            raise MaterialError("ONLINE_CITATION_MISMATCH")
        return result

    def inventory(self, identifier, query, *, operation="page"):
        with self.lock, self.store.transaction() as db:
            conversation = self._conversation(db, query.get("conversation_id"))
            workspace = self._workspace(db, conversation["workspace_id"])
            # Only a handle actually delivered into this history is public.
            if not any(message.get("inventory_result", {}).get("id") == identifier for message in conversation["messages"]):
                raise MaterialError("ONLINE_INVENTORY_NOT_FOUND")
        publication = self.inventory_store.publication(identifier, conversation_id=conversation["id"],
            workspace_id=workspace["id"], revision=workspace["revision"])
        if operation == "export":
            return publication.export()
        if operation == "evidence":
            page = publication.evidence(_text(query.get("citation_id"), 128, "ONLINE_INVALID_CITATION"))
            citation = page["citation"]
            with self.lock:
                if workspace["id"] not in self._credentials:
                    raise MaterialError("ONLINE_RECONNECT_REQUIRED")
                credentials = dict(self._credentials[workspace["id"]])
            service, source = self._session(workspace, credentials, self._require_open)
            result = service.evidence(citation["path"], citation["start"], citation["end"], source)
            if result["citation"]["hash"] != citation["hash"]:
                raise MaterialError("ONLINE_CITATION_MISMATCH")
            return result
        try:
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 40))
        except (ValueError, TypeError):
            raise MaterialError("ONLINE_INVALID_INVENTORY_PAGE") from None
        return publication.page(offset, limit)

    def releases(self, payload):
        from online_distribution import DownloadsClient
        workspace, repository = payload.get("workspace"), payload.get("repository")
        _repository(str(workspace) + "/" + str(repository))
        credentials = _credentials(payload.get("credentials"))
        client = DownloadsClient(workspace, repository, token=credentials.get("material_token"),
                                 username=credentials.get("material_username"), check_cancel=self._require_open)
        if hasattr(client, "list_release_entries"):
            entries = client.list_release_entries(max_pages=20, max_entries=1000)
            return {"release_names": [item["name"] for item in entries], "release_items": entries}
        return {"release_names": client.list_release_names()}

    def release(self, payload):
        from online_distribution import DownloadsClient, load_release
        location = _location({"kind": "downloads", "workspace": payload.get("workspace"),
                              "repository": payload.get("repository"), "release_name": payload.get("release_name")})
        credentials = _credentials(payload.get("credentials"))
        client = DownloadsClient(location["workspace"], location["repository"], token=credentials.get("material_token"),
                                 username=credentials.get("material_username"), check_cancel=self._require_open)
        descriptor = load_release(client, location["release_name"])
        return {"release": {"release_name": location["release_name"], "repository": descriptor["repository"],
                            "revision": descriptor["revision"], "source_basis": _source_basis(descriptor)}}

    def dispatch(self, method, path, payload=None, query=None):
        """Called only after the existing HTTP origin/session-token guard."""
        try:
            self._require_open()
            if not isinstance(payload, (dict, type(None))) or not isinstance(query, (dict, type(None))):
                raise MaterialError("ONLINE_INVALID_REQUEST")
            payload, query = payload or {}, query or {}
            if method == "GET":
                if path.startswith("/api/online/inventories/"):
                    parts = path.removeprefix("/api/online/inventories/").split("/")
                    if len(parts) == 1 or len(parts) == 2 and parts[1] in {"export", "evidence"}:
                        return 200, self.inventory(parts[0], query, operation=parts[1] if len(parts) == 2 else "page")
                    raise MaterialError("ONLINE_ROUTE_NOT_FOUND")
                if path == "/api/online/state":
                    return 200, self.state()
                if path.startswith("/api/online/jobs/"):
                    identifier = _identifier(path.removeprefix("/api/online/jobs/"))
                    with self.lock, self.store.transaction() as db:
                        return 200, {"job": self._get(db, "jobs", identifier)}
                if path.startswith("/api/online/conversations/"):
                    with self.lock, self.store.transaction() as db:
                        conversation = self._conversation(db, path.removeprefix("/api/online/conversations/"))
                        return 200, {"conversation": self._public_conversation(conversation)}
                if path == "/api/online/export":
                    return 200, self.export(query)
                if path == "/api/online/evidence":
                    return 200, self.evidence(query)
            if method == "POST":
                actions = {"connect": (202, self.connect), "update": (202, lambda value: self.connect(value, updating=True)),
                           "open": (200, self.open), "credentials": (200, self.credentials), "disconnect": (200, self.disconnect),
                           "conversations": (201, self.new_conversation), "question": (202, self.question),
                           "resume": (202, lambda value: self.question(value, resume=True)), "cancel": (200, self.cancel),
                           "releases": (200, self.releases), "release": (200, self.release)}
                action = path.removeprefix("/api/online/")
                if path.startswith("/api/online/") and action in actions:
                    status, function = actions[action]
                    return status, function(payload)
            if method == "DELETE" and path.startswith("/api/online/conversations/"):
                return 200, self.delete_conversation(path.removeprefix("/api/online/conversations/"))
            return 404, {"error": "ONLINE_ROUTE_NOT_FOUND"}
        except Exception as error:
            code = _safe_error(error)
            status = (404 if code.endswith("_NOT_FOUND") else 409 if code in {
                "ONLINE_BUSY", "ONLINE_RECONNECT_REQUIRED", "ONLINE_MATERIAL_REAUTH_REQUIRED", "ONLINE_PENDING_INVESTIGATION", "ONLINE_NOTHING_TO_RESUME", "ONLINE_CLOSED"}
                else 403 if code.endswith(("_FORBIDDEN", "_UNAUTHORIZED")) or code == "ONLINE_SOURCE_TRANSFER_REQUIRED"
                else 429 if code.endswith("_RATE_LIMIT") else 400 if code.startswith(("ONLINE_INVALID", "MATERIAL_INVALID", "SOURCE_INVALID"))
                else 502)
            return status, {"error": code, "diagnostic": sanitize_diagnostic(getattr(error, "diagnostic", None))}
