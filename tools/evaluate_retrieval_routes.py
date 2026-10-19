"""Compare real offline retrieval against literal synthetic source spans.

The cases are authored development probes, not independently reviewed gold or
production answer-quality measurements. No model, compiler or network is used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
from unittest import mock


LONG_QUESTION = (
    "请从入口请求开始追踪处理过程，解释记录读取之后怎样校验请求状态、计算最终结果、"
    "决定是否继续写回，并把默认值、参数来源、缺失数据、异常返回、失败分支、重复请求、"
    "批次边界、时间窗口、上下限条件、撤销操作、重新执行、调用关系、共享数据以及最终输出"
    "串成一条可以根据源码核对的流程。对于每一步都需要给出准确的条件和对应的代码位置，"
    "区分已经明确实现的判断和目前仍然缺少证据的情况。最后检查后续过程是否覆盖之前的结果，"
    "以及本次请求失败时已经发生的修改是否会被恢复。"
)
VERBOSE_HINT = (
    "需要进一步检查请求进入后的记录匹配过程，核对状态标记以及返回结果在不同处理分支中"
    "的变化，特别关注最初读入的默认值、后续计算使用的调整参数、最终提交前的条件判断，"
    "还需要找到数据缺失时候的替代路径以及重复执行时避免再次修改的代码，不能仅凭名称"
    "判断业务含义，应当沿着调用链查看实际的赋值和判断表达式。"
)


def program(name, body):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}\nGOBACK.\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path, nargs="?", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--module", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repository / "poc"))
    if args.module:
        spec = importlib.util.spec_from_file_location("repository_discovery", args.module)
        module = importlib.util.module_from_spec(spec)
        sys.modules["repository_discovery"] = module
        spec.loader.exec_module(module)
    import repository_discovery as retrieval
    from business_index import build_business_index
    from retrieval_quality_eval import evaluate_evidence

    scenarios = [
        {"id": "long-question-new-english-clue", "question": LONG_QUESTION,
         "search_terms": ["ORBITALALLOCATION"], "needle": "ORBITALALLOCATION",
         "target_body": "*> ORBITALALLOCATION applies only to the final adjustment.\nCOMPUTE FINAL-VALUE = BASE-VALUE + 37.",
         "distractor": "*> Ordinary unrelated source."},
        {"id": "verbose-first-clue-fair-budget", "question": LONG_QUESTION,
         "search_terms": [VERBOSE_HINT, "LANTERNCAPVALUE"], "needle": "LANTERNCAPVALUE",
         "target_body": "*> LANTERNCAPVALUE controls the approved amount.\nCOMPUTE FINAL-VALUE = BASE-VALUE + 43.",
         "distractor": "*> Ordinary unrelated source."},
        {"id": "exact-identifier-against-split-distractors", "question": "这个字段控制什么条件？",
         "search_terms": ["SETTLEMENT-CUTOFF-CODE"], "needle": "SETTLEMENT-CUTOFF-CODE",
         "target_body": "*> SETTLEMENT-CUTOFF-CODE controls the final branch.\n" + "\n".join(f"MOVE {n} TO WORK-VALUE-{n}." for n in range(220)),
         "distractor": "*> settlement cutoff code appear as separate generic words."},
    ]
    report = {"schema": "adaptive-retrieval-probe/v1", "scope": "authored synthetic retrieval regressions",
              "production_retrieval_executed": True, "final_answer_evaluated": False,
              "retrieval_module_sha256": hashlib.sha256(Path(retrieval.__file__).read_bytes()).hexdigest(),
              "independent_review": False, "model_calls": 0, "max_pages": 1, "max_chars": 12000, "cases": []}
    with mock.patch.object(socket, "create_connection", side_effect=AssertionError("Offline probe")), \
         mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Offline probe")), \
         tempfile.TemporaryDirectory(prefix="adaptive-retrieval-probe-") as temporary:
        root = Path(temporary)
        for case in scenarios:
            source = root / case["id"] / "source"
            source.mkdir(parents=True)
            target = source / "z-target.cbl"
            target.write_text(program("TARGET-ENTRY", case["target_body"]), encoding="utf-8")
            for n in range(3):
                (source / f"a-distractor-{n}.cbl").write_text(program(f"DISTRACTOR-{n}", case["distractor"]), encoding="utf-8")
            database = source.parent / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            retrieval.ensure_repository_search(database, source)
            discovery = retrieval.discover_repository(database, case["question"], search_terms=case["search_terms"], fallback_to_repository=False)
            context = retrieval.retrieve_repository_context(database, source, case["question"], search_terms=case["search_terms"], max_pages=1, max_chars=12000)
            lines = target.read_text(encoding="utf-8").splitlines()
            line = next(i for i, text in enumerate(lines, 1) if case["needle"] in text)
            gold = {"units": [{"id": "literal-target", "relative_path": "z-target.cbl", "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                              "start_line": line, "end_line": line, "source_text": lines[line - 1], "relevance": 3}],
                    "required_items": [{"id": "literal-target", "critical": True, "sufficient_sets": [["literal-target"]]}]}
            pages = context["pages"]
            non_orientation = [] if context["orientation_only"] else pages
            raw = evaluate_evidence(gold, pages, pages, k=1)
            usable = evaluate_evidence(gold, non_orientation, non_orientation, k=1)
            report["cases"].append({"id": case["id"], "question_characters": len(case["question"]),
                "search_terms": case["search_terms"], "question_only_terms": len(retrieval._tokens(case["question"])),
                "target_sha256": gold["units"][0]["source_sha256"], "target_line": line,
                "raw_returned_recall_at_1": raw["recall_at_k"], "non_orientation_recall_at_1": usable["recall_at_k"],
                "first_returned_path": pages[0]["relative_path"] if pages else None,
                "orientation_only": context["orientation_only"], "context_matched_pages": context["matched_page_count"],
                "context_matched_files": context["matched_file_count"], "discovery_matched_files": discovery["matched_file_count"],
                "discovery_target_selected": "z-target.cbl" in discovery["selected_paths"],
                "omitted_search_terms": context["omitted_search_terms"],
                "target_clue_retained": case["needle"].casefold() in context["search_terms"],
                "orientation_consistent_with_returned_literal": not (raw["recall_at_k"] and context["orientation_only"])})
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
