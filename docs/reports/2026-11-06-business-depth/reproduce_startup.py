import argparse
from contextlib import closing
from http.client import HTTPConnection
import inspect
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--modules", required=True)
parser.add_argument("--poc", required=True)
parser.add_argument("--background", action="store_true")
parser.add_argument("--output", required=True)
args = parser.parse_args()
sys.path[:0] = [args.modules, args.poc]
import report_view
import workspace_upgrade
import web_app
from analyze_source import analyze_source

with tempfile.TemporaryDirectory(prefix="startup-benchmark-") as directory:
    root = Path(directory).resolve()
    source, output = root / "source", root / "output"
    source.mkdir()
    (source / "rule.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-RULE.\nPROCEDURE DIVISION.\nDISPLAY 'ACCEPTED'.\nSTOP RUN.\n")
    analyze_source(source, output, source_format="free", analysis_mode="business", allow_network=False)
    database = output / "structural-index.sqlite"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE metadata SET value='business-sparse-v1.9' WHERE key='parser_version'")
    report_path = output / "diagnosis.json"
    report = json.loads(report_path.read_text())
    report["retained_details"] = "x" * (64 * 1024 * 1024)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    report_view.write_report_view(report_path, report)
    profile = root / "state.json"
    profile.write_text(json.dumps({"source": str(source), "output": str(output)}))
    original_hash = report_view.report_sha256
    hash_calls = []
    completed = threading.Event()
    worker_errors = []

    def measured_hash(path, **options):
        size = path.stat().st_size
        hash_calls.append({"bytes": size, "name": path.name})
        # Model a slow external/storage scan explicitly, rather than claiming
        # that a warm local SSD reproduces the office machine's throughput.
        delay = size / (16 * 1024 * 1024)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if options.get("check_cancel"):
                options["check_cancel"]()
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return original_hash(path, **options)

    def verify_upgrade(self, identifier, options):
        try:
            upgrade = options["_index_upgrade"]
            kwargs = {}
            if "report_session" in inspect.signature(workspace_upgrade.plan_workspace_upgrade).parameters:
                kwargs["report_session"] = upgrade.get("report_session")
            result = workspace_upgrade.plan_workspace_upgrade(upgrade["saved"], **kwargs)
            if result["status"] != "ready":
                raise AssertionError(result)
            with self.lock:
                self.job["status"] = "COMPLETED"
                self.updated_at = time.monotonic()
        except BaseException as exc:
            worker_errors.append(type(exc).__name__)
        finally:
            completed.set()

    with patch("report_view.report_sha256", side_effect=measured_hash), \
            patch("web_app.report_sha256", side_effect=measured_hash), \
            patch("workspace_upgrade.report_sha256", side_effect=measured_hash), \
            patch.object(web_app.WorkbenchState, "_run", verify_upgrade):
        started = time.monotonic()
        app = web_app.WorkbenchState(state_path=profile, config_provider=lambda: SimpleNamespace(validate=lambda: None),
            **({"background_restore": True} if args.background else {}))
        constructed = time.monotonic() - started
        server = web_app.create_server(0, app=app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            first_state = json.loads(response.read())
            http_ready = time.monotonic() - started
            connection.close()
            if not completed.wait(45):
                raise AssertionError("worker timed out")
            full_ready = time.monotonic() - started
            if worker_errors:
                raise AssertionError(worker_errors)
            if args.background:
                app._startup_restore_thread.join(5)
            result = {"schema":"startup-recovery-benchmark/v1", "background_restore": args.background,
                "report_bytes": report_path.stat().st_size, "simulated_read_bytes_per_second": 16 * 1024 * 1024,
                "constructor_seconds": round(constructed,4), "first_http_seconds":round(http_ready,4),
                "restore_and_upgrade_provenance_seconds":round(full_ready,4),
                "full_report_hash_calls":len(hash_calls), "full_report_hash_bytes":sum(row["bytes"] for row in hash_calls),
                "initial_status":first_state["job"]["status"], "initial_origin":first_state["job"].get("origin"),
                "initial_snapshot_available":bool(first_state["project"]["snapshot_id"]),
                "final_snapshot_available":bool(app.project["snapshot_id"]),
                "checks":{"http_200":response.status==200,"upgrade_provenance_valid":not worker_errors},
                "scope":"Synthetic source and slow-read simulation; unchanged report and real restore/planning code. Index rebuild, model calls, office-device timings excluded."}
            Path(args.output).write_text(json.dumps(result, indent=2)+"\n")
            print(json.dumps(result))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
