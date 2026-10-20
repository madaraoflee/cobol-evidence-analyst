"""Business onboarding through the real source or compiled desktop HTTP path."""
import time

from tools.business_onboarding_fixture import (BATCH_QUESTION, ONLINE_QUESTION,
    fixture_identity, scripted_answer, sources)
from tools.desktop_acceptance_runtime import (LoopbackProvider, NativeWorkbench,
    decoded_navigation, require, supplied_pages, write_profile)


def run_business_overview(command, root, case, sample_index):
    from tools.desktop_acceptance_scenarios import _checked_answer, _index, _row
    source, output = root / "source", root / "workspace"
    source.mkdir()
    for path, text in sources().items():
        (source / path).write_bytes(text.encode())
    expected = fixture_identity()
    batch = case["id"] == "business_batch_overview"
    question = BATCH_QUESTION if batch else ONLINE_QUESTION
    def respond(envelope, payload, index):
        require(index == 0, "Supplied business overview should answer in one request")
        require(payload["question"] == question, "Wrong onboarding question")
        orientation = payload.get("business_analysis_brief", {}).get("business_orientation", {})
        require({"business_purpose", "program_roles", "data_roles"} <= set(orientation.get("available_facets", [])),
                "Business roles were not supplied")
        require(payload.get("response_contract", {}).get("business_explanation", {}).get("audience"),
                "Newcomer business explanation was not requested")
        if batch:
            require(any(row.get("command") == "CALL" for row in orientation.get("relationships", [])),
                    "Known batch entry was not supplied before the answer")
        else:
            require(any(row.get("kind") == "sql_table_access" and row.get("table_name") == "ACCOUNT_BALANCES"
                        and row.get("access_role") == "read" for row in orientation.get("data_roles", [])),
                    "SQL data role was not supplied")
        return scripted_answer(payload)
    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider, case["policy"])
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            cold = _index(workbench, source, output, provider)
            observation = workbench.analyze(source, output, question, answer_detail="detailed")
            agent = _checked_answer(observation, provider)
            payload = provider.requests[0]["payload"]
            pages = supplied_pages(payload)
            orientation = decoded_navigation(payload).get("business_analysis_brief", {}).get("business_orientation", {})
            raw_ok = all(page.get("source_sha256") == expected.get(page.get("relative_path"))
                and page.get("source_text") == "\n".join(sources()[page["relative_path"]].splitlines()[
                    page["start_line"] - 1:page["end_line"]]) and not page.get("span_truncated") for page in pages)
            visible = observation["state"]["conversation"]["messages"][-1]["content"]
            answer = agent["answer"]
            return [_row(case, sample_index, observation, provider, cold=cold, checks={
                "quality_business_purpose_and_data_roles_preserved": scripted_answer(payload) in answer,
                "quality_runtime_unknowns_remain_local": ("调度或覆盖实际生效" in answer if batch else "交易绑定" in answer),
                "completeness_roles_supplied_before_answer": {"business_purpose", "program_roles", "data_roles"}
                    <= set(orientation.get("available_facets", [])),
                "completeness_one_model_request": len(provider.requests) == 1,
                "evidence_raw_source_and_versions_match": raw_ok,
                "evidence_visible_answer_matches": visible == answer and bool(agent.get("evidence_refs")),
                "lifecycle_default_has_no_fixed_total_deadline": agent["metrics"]["policy"]["hard_question_seconds"] is None
                    and agent["metrics"]["policy"]["max_model_requests"] == 6})]
