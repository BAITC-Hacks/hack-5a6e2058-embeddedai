"""The model can request a computation but cannot invent its graph evidence."""

import json
import math
from copy import deepcopy

import pytest

from money_graph.graph_query import execute_query
from money_graph.loader import DataError


@pytest.fixture
def graph_result():
    sources = ["-5", "2", "3", "4", "9007199254740993"]
    gids = sources + ["10", "100", "200", "300", "400"]
    edges = (
        [
            {"src": source, "dst": "100", "sum_kzt": index * 5_000.0, "n_tx": index}
            for index, source in enumerate(sources, 1)
        ]
        + [{"src": source, "dst": "200", "sum_kzt": 100_000.0, "n_tx": 2} for source in sources[:4]]
        + [
            {"src": src, "dst": dst, "sum_kzt": 5_000.0, "n_tx": 1}
            for src, dst in [
                ("100", "300"),
                ("200", "300"),
                ("300", "400"),
                ("400", "100"),
                ("100", "2"),
                ("3", "2"),
            ]
        ]
    )
    nodes = []
    for rank, gid in enumerate(gids, 1):
        incoming = [edge for edge in edges if edge["dst"] == gid]
        outgoing = [edge for edge in edges if edge["src"] == gid]
        n = {
            "gid": gid,
            "role": "consolidator" if gid in ("100", "200") else "peripheral",
            "priority_score": 0.5,
            "rank": rank,
            "in_kzt": math.fsum(edge["sum_kzt"] for edge in incoming),
            "out_kzt": math.fsum(edge["sum_kzt"] for edge in outgoing),
            "in_deg": len(incoming),
            "out_deg": len(outgoing),
            "in_tx": sum(edge["n_tx"] for edge in incoming),
            "out_tx": sum(edge["n_tx"] for edge in outgoing),
            "is_seed": gid in sources,
            "boundary_censored": False,
            "cluster_id": 1 if gid in sources else 2,
            "evidence": f"Наблюдаемые рёбра узла {gid}.",
        }
        n["volume"] = max(n["in_kzt"], n["out_kzt"])
        nodes.append(n)
    return {
        "nodes": nodes,
        "edges": edges,
        "clusters": [
            {"cluster_id": 1, "hypothesis": "Группа отправителей, структурная гипотеза."},
            {"cluster_id": 2, "hypothesis": "Группа получателей, структурная гипотеза."},
        ],
    }


def test_five_sources_finds_actual_collector_with_exact_aggregated_money(graph_result):
    plan = {"operation": "common_recipients", "gids": ["-5", "2", "3", "4", "9007199254740993"]}
    answer = execute_query(graph_result, plan)
    assert [fact["gid"] for fact in answer["facts"]] == ["100"]
    collector = answer["facts"][0]
    assert collector["matched_count"] == 5
    assert collector["direct_sum_kzt"] == 75_000
    assert collector["direct_n_tx"] == 15
    assert len(collector["paths"]) == 5
    assert "5 из 5" in answer["claims"][0]["text"]
    assert "75 000.00 KZT" in answer["claims"][0]["text"]
    assert {node["gid"] for node in answer["nodes"]} == {"100", *plan["gids"]}
    assert "9007199254740993" in json.dumps(answer)


def test_partial_common_sources_rank_coverage_before_amount_and_keep_selected_candidates(
    graph_result,
):
    plan = {
        "operation": "common_recipients",
        "gids": ["-5", "2", "3", "4", "9007199254740993"],
        "min_sources": 4,
    }
    answer = execute_query(graph_result, plan)
    assert [fact["gid"] for fact in answer["facts"]] == ["100", "200"]
    assert answer["facts"][0]["direct_sum_kzt"] < answer["facts"][1]["direct_sum_kzt"]
    selected_candidate = execute_query(
        graph_result, {"operation": "common_recipients", "gids": ["2", "3"], "min_sources": 1}
    )
    assert "2" in {fact["gid"] for fact in selected_candidate["facts"]}
    two = next(fact for fact in selected_candidate["facts"] if fact["gid"] == "2")
    assert two["matched_sources"] == ["3"]


def test_common_senders_reverse_search_returns_edges_in_actual_money_direction(graph_result):
    result = execute_query(graph_result, {"operation": "common_senders", "gids": ["100", "200"]})
    assert {fact["gid"] for fact in result["facts"]} == {"-5", "2", "3", "4"}
    for fact in result["facts"]:
        assert fact["matched_count"] == 2
        assert {path["gids"][0] for path in fact["paths"]} == {fact["gid"]}
        assert {path["gids"][-1] for path in fact["paths"]} == {"100", "200"}
        for path in fact["paths"]:
            assert path["edges"][0]["src"] == fact["gid"]


def test_multihop_matches_have_bounded_proven_paths_without_fabricated_through_amount(graph_result):
    result = execute_query(
        graph_result,
        {"operation": "common_recipients", "gids": ["-5", "9007199254740993"], "max_hops": 2},
    )
    sink = next(fact for fact in result["facts"] if fact["gid"] == "300")
    assert "direct_sum_kzt" not in sink and "sum_kzt" not in sink
    assert all(path["gids"][-1] == "300" and len(path["gids"]) == 3 for path in sink["paths"])
    assert "Сквозная сумма" in result["limitations"][-1]
    assert "100" in {node["gid"] for node in result["nodes"]}
    real_edges = {(edge["src"], edge["dst"]): edge for edge in graph_result["edges"]}
    for fact in result["facts"]:
        for path in fact["paths"]:
            assert len(path["gids"]) - 1 <= 2
            for edge in path["edges"]:
                assert edge == real_edges[(edge["src"], edge["dst"])]


def test_shortest_path_is_directed_bounded_and_ties_use_numeric_ids(graph_result):
    answer = execute_query(
        graph_result, {"operation": "path", "gids": ["-5", "300"], "max_hops": 2}
    )
    assert answer["facts"][0]["paths"][0]["gids"] == ["-5", "100", "300"]
    assert answer["facts"][0]["hops"] == 2
    reverse = execute_query(
        graph_result, {"operation": "path", "gids": ["300", "-5"], "max_hops": 4}
    )
    assert reverse["facts"] == []
    assert "не найден" in reverse["answer"]
    too_short = execute_query(
        graph_result, {"operation": "path", "gids": ["-5", "300"], "max_hops": 1}
    )
    assert too_short["facts"] == []
    duplicate = execute_query(graph_result, {"operation": "path", "gids": ["100", "100"]})
    assert "два разных" in duplicate["answer"]


def test_cycles_are_real_bounded_and_rotation_duplicates_do_not_repeat(graph_result):
    answer = execute_query(
        graph_result, {"operation": "cycles", "gids": ["100", "2", "300", "400"], "max_hops": 3}
    )
    assert len(answer["facts"]) == 2
    assert sorted(fact["hops"] for fact in answer["facts"]) == [2, 3]
    for fact in answer["facts"]:
        path = fact["paths"][0]
        assert path["gids"][0] == path["gids"][-1]
        assert len(path["edges"]) == fact["hops"]
    assert "не полное перечисление" in answer["limitations"][-1]


def test_rank_role_filter_exact_values_and_numeric_ties(graph_result):
    result = execute_query(
        graph_result, {"operation": "rank_nodes", "role": "consolidator", "sort_by": "volume"}
    )
    assert [fact["gid"] for fact in result["facts"]] == ["200", "100"]
    expected = next(row["volume"] for row in graph_result["nodes"] if row["gid"] == "200")
    assert result["facts"][0]["value"] == expected
    tied = execute_query(
        graph_result,
        {"operation": "rank_nodes", "gids": ["10", "2", "9007199254740993"], "limit": 2},
    )
    assert [fact["gid"] for fact in tied["facts"]] == ["2", "10"]
    absent = execute_query(graph_result, {"operation": "rank_nodes", "role": "transit"})
    assert absent["facts"] == []
    assert "0 из 0" in absent["answer"]


def test_node_summary_and_community_return_only_actual_records(graph_result):
    result = execute_query(graph_result, {"operation": "node_summary", "gids": ["100"]})
    hundred = next(row for row in graph_result["nodes"] if row["gid"] == "100")
    fact = result["facts"][0]
    assert all(value == hundred[key] for key, value in fact.items() if key != "kind")
    community = execute_query(
        graph_result, {"operation": "community", "gids": ["100", "200"], "limit": 3}
    )
    assert len(community["facts"]) == 1
    assert community["facts"][0]["n_nodes"] == 5
    assert community["facts"][0]["top_gids"] == ["10", "100", "200"]


def test_no_results_missing_selection_and_unsupported_never_fabricate_nodes(graph_result):
    for operation in (
        "common_recipients",
        "common_senders",
        "node_summary",
        "community",
        "path",
        "cycles",
    ):
        answer = execute_query(graph_result, {"operation": operation})
        assert answer["answer"]
        assert answer["facts"] == answer["nodes"] == answer["claims"] == []
    empty = execute_query(graph_result, {"operation": "common_recipients", "gids": ["10", "100"]})
    assert empty["facts"] == [] and "не найдено" in empty["answer"]
    unsupported = execute_query(
        graph_result,
        {"operation": "unsupported", "clarification": "Invented node 999 committed a crime"},
    )
    assert "999" not in unsupported["answer"]
    assert unsupported["facts"] == []


@pytest.mark.parametrize(
    "fields",
    [
        {"gids": ["unknown"]},
        {"gids": [9007199254740993]},
        {"limit": 21},
        {"max_hops": 5},
        {"limit": True},
        {"min_sources": -1},
        {"role": "fraud"},
        {"sort_by": "password"},
        {"gids": ["100"] * 21},
    ],
)
def test_invalid_plans_are_rejected_before_graph_work(graph_result, fields):
    with pytest.raises(DataError):
        execute_query(graph_result, {"operation": "node_summary"} | fields)


def test_queries_preserve_result_and_plan_and_are_repeatable(graph_result):
    original = deepcopy(graph_result)
    for operation in (
        "common_recipients",
        "common_senders",
        "node_summary",
        "rank_nodes",
        "community",
        "path",
        "cycles",
        "unsupported",
    ):
        plan = {"operation": operation, "gids": ["100", "300"], "max_hops": 3}
        before = deepcopy(plan)
        first = execute_query(graph_result, plan)
        assert first == execute_query(graph_result, plan)
        json.dumps(first, allow_nan=False)
        assert plan == before
    assert graph_result == original


def test_duplicate_selected_sources_do_not_inflate_match_count(graph_result):
    result = execute_query(
        graph_result, {"operation": "common_recipients", "gids": ["2", "2", "3"]}
    )
    fact = next(row for row in result["facts"] if row["gid"] == "100")
    assert fact["matched_count"] == 2
    assert fact["direct_n_tx"] == 5


def test_direct_totals_use_compensated_sum(graph_result):
    result = deepcopy(graph_result)
    source = [edge for edge in result["edges"] if edge["dst"] == "100"][:3]
    source[0]["sum_kzt"], source[1]["sum_kzt"], source[2]["sum_kzt"] = 1e16, 1.0, 1.0
    answer = execute_query(
        result, {"operation": "common_recipients", "gids": [edge["src"] for edge in source]}
    )
    assert answer["facts"][0]["direct_sum_kzt"] == 1e16 + 2
