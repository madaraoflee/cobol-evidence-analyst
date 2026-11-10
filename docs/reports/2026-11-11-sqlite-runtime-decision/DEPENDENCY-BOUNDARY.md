# SQLite runtime readiness, investigation only

The least invasive candidate is a pinned application-private SQLite component upgrade, after the isolated WAL experiment demonstrates a worthwhile benefit. It is still a shared local-and-online runtime change and requires a new explicit scope decision under the local freeze. No runtime has been replaced or loaded, no repository file changed, and no application compiled by this investigation.

## Official artifact and fix boundary

The official [WAL-reset description](https://sqlite.org/wal.html#11) identifies 3.50.4 as affected and records the 3.51.3 fix plus the 3.50.7 backport. The project's observed Windows engine is 3.50.4. That is an upstream applicability fact, not evidence that this application has experienced corruption.

| Candidate | What is established | Remaining limit |
|---|---|---|
| 3.50.7 | Official backport branch check-in c7facf7ac58d2cdac619a2da838c5fd5c5270f52b3458c7881b5a4fca1ca4791; conventional Windows x64 ZIP URL returns 404. | No official published x64 binary/checksum located in this investigation. A source build would require its own compiler/options/provenance verification. The check-in hash is not a verified DLL source ID. |
| 3.53.2 | Historical official x64 ZIP still returns 200; release source ID is 2026-06-03 19:12:13 d6e03d8c777cfa2d35e3b60d8ec3e0187f3e9f99d8e2ee9cac695fd6fcdf1a24. | Not current latest; later 3.53.3/3.53.4 fixes exist. Historical archive was not downloaded or independently matched to a published archive checksum. |
| 3.54.1 | Current official download page; Windows x64 archive downloaded to tmp only and matched official SHA3-256. PE machine 0x8664, official source ID string and selected C exports present. | No Windows CPython load, full import-table check, compile-options or final EXE compatibility result. |

Pinned 3.54.1 candidate URL: https://sqlite.org/2026/sqlite-dll-win-x64-3540100.zip

Official archive SHA3-256: c2d09c8babb9399277055c057c97a53f1ffd7c5f11d0c1f2118ffdfa39f2daa4

Downloaded DLL SHA256: 4b59d3cded9dc1608544160f3b4b10ac974c6ac0d6ffc23064e5f4c8dbd05aea

Official source ID, also present in downloaded DLL bytes: 2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76

The [official download page](https://sqlite.org/download.html) and [3.54.1 release record](https://sqlite.org/releaselog/3_54_1.html) provide independent published archive and source identities. The downloaded ZIP contains sqlite3.dll and sqlite3.def only. Nothing from it was installed or executed. Exact checks are in archive-static-verification.json and download-availability.json.

## CPython and Nuitka boundary

Stock non-embedded Windows CPython 3.13 places _sqlite3.pyd and sqlite3.dll under its base DLLs directory; python313.dll is at the base root. The observed job used C:\\hostedtoolcache\\windows\\Python\\3.13.17\\x64. This layout follows the [CPython 3.13.17 layout generator](https://github.com/python/cpython/blob/v3.13.17/PC/layout/main.py#L195); the exact loaded SQLite DLL path has not yet been collected from Windows. Embedded/flat layouts place these files differently and must not be inferred from the ordinary installer layout.

A normal venv keeps a reference to base Python in pyvenv.cfg. It does not provide a separate complete standard library/native SQLite runtime. See [venv](https://docs.python.org/3.13/library/venv.html) and [the Windows venv implementation](https://github.com/python/cpython/blob/v3.13.17/Lib/venv/__init__.py#L296). Copying a DLL into venv/DLLs or altering PATH is therefore not proof that _sqlite3 will use it.

SQLite's [3.x compatibility policy](https://sqlite.org/versionnumbers.html) supports retaining the existing CPython 3.13 _sqlite3.pyd and changing the C core, but this is an ABI compatibility expectation that still needs an actual Windows test. The [CPython extension project](https://github.com/python/cpython/blob/v3.13.17/PCbuild/_sqlite3.vcxproj#L130) links the SQLite dynamic library; the [engine project](https://github.com/python/cpython/blob/v3.13.17/PCbuild/sqlite3.vcxproj#L98) enables FTS4, FTS5, RTREE and math functions. Matching x64, required C exports, thread mode, JSON/FTS and serialization APIs remains necessary. A static source-ID string is not a successful dynamic load test.

Repository build facts:

- desktop-requirements.txt:2 pins Nuitka 4.2.2; SQLite is not a pip dependency.
- build_app.py:184 creates/reuses the build venv; 336–339 re-enters that interpreter; 119–128 invokes Nuitka and produces compilation-report.xml.
- Nuitka relies on [generic DLL dependency scanning](https://github.com/Nuitka/Nuitka/blob/4.2.2/nuitka/freezer/DllDependenciesWin32.py#L489), [copies the chosen dependency](https://github.com/Nuitka/Nuitka/blob/4.2.2/nuitka/freezer/Standalone.py#L255), and [reports its source_path and dest_path](https://github.com/Nuitka/Nuitka/blob/4.2.2/nuitka/reports/Reports.py#L1029). Current project gates do not check that SQLite report entry or record its version/source ID/DLL digest in BUILD-INFO.
- Existing venv reuse must not silently retain the old base when a runtime candidate changes.

## Proposed smallest shared-runtime diff, not implemented

1. Add a small tracked runtime manifest, for example desktop-sqlite-runtime.json, with the official URL, archive SHA3-256, extracted DLL SHA256, exact version/source ID and x64 requirement. Pin the artifact, not merely a minimum version.
2. Add a preparation helper that copies the selected unmodified CPython 3.13.17 base into a new private build root under RUNNER_TEMP, verifies the original source files, and replaces only the private DLLs/sqlite3.dll after archive/PE/export verification. Do not edit the hosted interpreter, Windows system directories or an existing venv. Use real copies, not hard links, and create a fresh venv from that private Python.
3. Wire the candidate source regressions and build to the same private interpreter. Use a new build-root/runtime fingerprint so cached old venvs cannot be reused. Original desktop 45-minute regression gate and all existing test/acceptance thresholds remain unchanged.
4. Extend build preparation and BUILD-INFO with SQLite identity. Validate Nuitka's included DLL source_path/dest_path and exact packaged bytes. In the final EXE self-test collect sqlite_version(), sqlite_source_id(), compile_options, _sqlite3 module identity and the Windows loaded DLL path/hash. A C API success probe must include JSON, FTS5, backup, serialize/deserialize, transaction rollback, and setconfig/getconfig for NO_CKPT_ON_CLOSE.
5. Keep online WAL policy as its own reviewable diff. Enabling it must require the pinned fixed engine; it must retain FULL and verify recovery/checkpoint lifecycle. This document does not authorize a production policy change or make the engine upgrade itself a performance result.

The new helper/manifest, build_app.py, the manually invoked Windows workflow, identity/self-test collection and corresponding new regression tests are the concrete preparation surface. A new engine also requires all recorded bug regressions, full Windows Python/browser checks, final packaged native contracts/GLOBAL coverage, and same-package real-model and office acceptance before a formal release. Old unverified field defects stay open.

## Shared reach and independent online alternative

Read-only AST inspection found 55 top-level poc files importing sqlite3, including four online_* modules. This is not a claim that all other 51 files are local core. Explicit frozen local users already set WAL in structural_index.py:1733 and source_catalog.py:47. Updating the common DLL changes their engine even if their source and journal policy are untouched. That shared runtime change is the reason a new user scope decision is necessary.

The online inventory's narrow connection boundary is online_inventory_store.py:51–72; online_state.py:30–53 is a separate online history database. An independent online engine could be connected here, but it must actually be separate. A Python wrapper that calls the same _sqlite3, sys.modules monkeypatching, or two folders containing identically named sqlite3.dll are not reliable engine isolation. [Windows checks already loaded module names](https://learn.microsoft.com/en-us/windows/win32/dlls/dynamic-link-library-search-order), including dependent DLLs.

A genuine same-process option needs a private extension built with a statically linked audited SQLite core or uniquely named core DLL, its own module initializer and DB-API exception/Row/connection behavior, plus explicit Nuitka inclusion and dual-engine identity tests. A separate private Python worker avoids DLL-name collision but adds process/IPC/cancellation/recovery contracts. Both avoid changing local SQLite, at a materially larger implementation and regression surface than one shared component update. No unverified third-party binary wheel is assumed.

## Rollback and remaining decision

Keep the old interpreter/build root and previously accepted ZIP immutable. Produce a new candidate package and new identity; never replace a DLL inside an already accepted ZIP and reuse its old acceptance. For isolated experiments, use independent neutral databases. If an online WAL policy is later deployed, quiesce writers/connections and complete a verified checkpoint/journal transition before reverting to a rollback-journal policy. Never delete live WAL/SHM files to force a rollback. Preserve a consistent prior snapshot and prove prior publication/restart behavior on that snapshot.

Recommendation if the isolated WAL experiment shows sufficient benefit: ask for explicit authorization covering the application-private shared engine component, its build/identity checks and regression of all existing SQLite users. The concrete available artifact is the verified 3.54.1 DLL, not an assumed 3.50.7 binary. If local runtime change is declined, budget a separate online engine/worker rather than claiming a wrapper or venv is isolation. No approval is being requested by this read-only preparation report.
