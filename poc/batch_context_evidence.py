"""Supply indexed batch commands without making runtime context a result gate."""
from __future__ import annotations

from pathlib import PurePosixPath

from cl_context import bind_indexed_cl_context, is_cl_path, nominate_cl_evidence


def investigate_batch_context(business_map, pages, *, database_path=None,
                              max_actions=4, candidate_cache=None):
    """Read only nominated paths; recheck every observation against sent pages.

    The caller can attach these facts and optional reads to an existing plan.
    No missing job configuration invalidates independently supported answers.
    """
    roots = tuple(dict.fromkeys(business_map.get("direct_paths", ())))[:8]
    selected = roots or tuple(dict.fromkeys(business_map.get("selected_paths", ())))[:8]
    names = tuple(dict.fromkeys(name for row in business_map.get("programs", ())
        if row.get("relative_path") in selected for name in row.get("program_names", ()) if name))
    # DDS/CL source-member names are candidates, never confirmed object binding.
    names = tuple(dict.fromkeys([*names, *(PurePosixPath(path).stem.upper()
        for path in selected if is_cl_path(path) or PurePosixPath(path).suffix.lower() in {".dds", ".lf", ".pf"})]))
    key = (str(database_path), business_map.get("snapshot_id"), selected, names)
    cache = candidate_cache if isinstance(candidate_cache, dict) else {}
    if cache.get("batch_context_key") == key:
        candidates, frontier, hashes = cache["batch_context_candidates"]
    elif database_path and selected:
        candidates, frontier, hashes = nominate_cl_evidence(database_path, selected, names)
        cache.update(batch_context_key=key, batch_context_candidates=(candidates, frontier, hashes))
    else:
        candidates, frontier, hashes = [], [], {}
    scoped_paths = {row["relative_path"] for row in candidates}
    context = bind_indexed_cl_context([page for page in pages if page.get("relative_path") in scoped_paths
        and page.get("source_sha256") == hashes.get(page.get("relative_path"))], candidates)
    supplied = {(row["relative_path"], row["source_sha256"], row["occurrence_id"])
        for row in context["observations"] if row.get("evidence_ids")}
    missing = [row for row in candidates if not all((row["relative_path"], row["source_sha256"], occurrence)
        in supplied for occurrence in row.get("occurrence_ids", [row["occurrence_id"]]))]
    # Multiple commands in one bounded range share one source read, while
    # individual statements and their source occurrence identities stay intact.
    ranges = {}
    for row in missing:
        first, last = row["start_line"], row["end_line"]
        if type(row.get("file_line_count")) is int:
            last = min(row["file_line_count"], last + 4)
        bucket = (first - 1) // 192
        key = row["relative_path"], bucket
        start = 1 if bucket == 0 else first
        previous = ranges.get(key)
        ranges[key] = (min(start, previous[0]) if previous else start,
                       max(last, previous[1]) if previous else last)
    reads = [{"tool": "read", "arguments": {"relative_path": path, "start_line": first,
        "end_line": last}, "reason": "indexed_batch_context_not_supplied"}
        for (path, _), (first, last) in ranges.items()]
    count = max(0, int(max_actions))
    context["frontier"] = list(frontier)
    if len(reads) > count:
        context["frontier"].append({"reason": "batch_context_read_frontier", "remaining_ranges": len(reads)-count})
    context["candidate_count"] = len(candidates)
    context["unread_candidate_count"] = len(missing)
    context["binding_basis"] = "indexed_target_and_source_member_candidates_not_live_object_resolution"
    context["interpretation"] = ("根据各命令自己的 evidence_ids 解释提交、调用、库列表和文件覆盖声明；"
        "缺少条件或环境前驱证据时保留候选范围，不断言无条件执行。提交方覆盖不等于子作业继承；"
        "实际调度、目标对象、成员变量值和覆盖生效须有运行配置。未知只限定相关结论。")
    return {"cl_context": context, "planned_actions": reads[:count]}
