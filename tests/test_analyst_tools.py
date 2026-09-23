"""The conversational model gets bounded evidence, never a graph mutation API."""

import json
from copy import deepcopy

import pytest

from money_graph.analyst_tools import MAX_OUTPUT_CHARS, TOOL_SCHEMAS, execute_tool
from money_graph.demo import create_demo
from money_graph.loader import DataError
from money_graph.pipeline import analyze


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    source = tmp_path_factory.mktemp("analyst-tools")
    create_demo(source)
    return analyze(source)


def find_args(**changes):
    return {
        "gids": [],
        "role": None,
        "depth": None,
        "cluster_id": None,
        "seeds_only": False,
        "anomaly_only": False,
        "min_in_kzt": None,
        "min_out_kzt": None,
        "min_volume": None,
        "sort_by": "priority_score",
        "order": "desc",
        "limit": 10,
    } | changes


def inspect_args(gids, **changes):
    return {
        "gids": gids,
        "detail": "overview",
        "date_from": None,
        "date_to": None,
        "limit": 20,
    } | changes


def test_schemas_have_strict_all_required_contract():
    assert len(TOOL_SCHEMAS) == 6
    assert len({tool["name"] for tool in TOOL_SCHEMAS}) == 6
    for tool in TOOL_SCHEMAS:
        assert tool["strict"] is True
        schema = tool["parameters"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize("topic", ["overview", "quality", "methodology"])
def test_graph_facts_are_exact_and_independent_of_result_mutation(result, topic):
    original = deepcopy(result)
    response = execute_tool(result, "inspect_graph", {"topic": topic})
    if topic == "methodology":
        assert response["facts"][0]["rules"] == result["report"]["rules"]
        response["facts"][0]["rules"]["coordinator_min_in"] = 999
    else:
        assert response["facts"][0]["turnover_kzt"] == result["report"]["turnover_kzt"]
        assert response["facts"][0]["n_nodes"] == len(result["nodes"])
        response["facts"][0]["role_counts"].clear()
    assert result == original


def test_find_respects_every_filter_and_reports_full_count(result):
    altered = deepcopy(result)
    nodes = altered["nodes"][:5]
    altered["nodes"] = nodes
    for index, node in enumerate(nodes):
        node.update(
            gid=str(index + 1),
            role="transit",
            depth=2,
            cluster_id=3,
            is_seed=True,
            in_kzt=100,
            out_kzt=200,
            volume=200,
        )
        node["anomaly_profile"]["signals"] = [{"metric": "volume"}]
    nodes[0]["role"] = "terminal"
    nodes[1]["anomaly_profile"]["signals"] = []
    nodes[2]["is_seed"] = False
    nodes[4]["out_kzt"] = 100
    response = execute_tool(
        altered,
        "find_nodes",
        find_args(
            role="transit",
            depth=2,
            cluster_id=3,
            seeds_only=True,
            anomaly_only=True,
            min_in_kzt=100,
            min_out_kzt=150,
            min_volume=200,
        ),
    )
    assert response["facts"][0]["matched_count"] == 1
    assert [node["gid"] for node in response["nodes"]] == ["4"]
    for extra in ({"depth": 1}, {"cluster_id": 9}, {"min_in_kzt": 101}, {"min_volume": 201}):
        empty = execute_tool(altered, "find_nodes", find_args(gids=["4"], **extra))
        assert empty["facts"][0]["matched_count"] == 0


def test_rank_subset_keeps_exact_int64_and_numeric_ties(result):
    altered = deepcopy(result)
    altered["nodes"] = altered["nodes"][:3]
    for node, gid in zip(altered["nodes"], ["9007199254740993", "-5", "10"], strict=True):
        node.update(gid=gid, priority_score=0.5)
    answer = execute_tool(altered, "find_nodes", find_args(limit=2))
    assert [node["gid"] for node in answer["nodes"]] == ["-5", "10"]
    assert answer["facts"][0]["matched_count"] == 3
    subset = execute_tool(altered, "find_nodes", find_args(gids=["9007199254740993"]))
    assert subset["nodes"][0]["gid"] == "9007199254740993"


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 21},
        {"depth": 5},
        {"depth": 1.0},
        {"role": "criminal"},
        {"gids": [3]},
        {"gids": ["unknown"]},
        {"seeds_only": "false"},
        {"min_in_kzt": -1},
        {"min_out_kzt": float("nan")},
        {"min_volume": float("inf")},
        {"cluster_id": 10**500},
        {"sort_by": "eval(1)"},
        {"order": "sideways"},
        {"unrecognized_filter": 3},
    ],
)
def test_invalid_filters_are_rejected_not_ignored(result, changes):
    with pytest.raises(DataError):
        execute_tool(result, "find_nodes", find_args(**changes))


def test_unknown_tool_missing_argument_duplicate_and_empty_gids_fail(result):
    gid = result["nodes"][0]["gid"]
    for name, args in [
        ("run_python", {}),
        ("inspect_graph", {}),
        ("inspect_nodes", inspect_args([gid, gid])),
        ("inspect_nodes", inspect_args([])),
        ("assess_removal", {"gids": []}),
    ]:
        with pytest.raises(DataError):
            execute_tool(result, name, args)


@pytest.mark.parametrize(
    "detail,key",
    [
        ("overview", "next_checks"),
        ("role", "rule_trace"),
        ("anomaly", "anomaly_profile"),
        ("next_checks", "warnings"),
        ("compare", "priority_parts"),
    ],
)
def test_node_profiles_provide_saved_explanations_and_comparison_metrics(result, detail, key):
    row = result["nodes"][0]
    answer = execute_tool(result, "inspect_nodes", inspect_args([row["gid"]], detail=detail))
    assert answer["facts"][0][key] == row[key]
    assert answer["nodes"][0]["gid"] == row["gid"]


def test_temporal_range_totals_are_exact_and_not_full_period(result):
    row = next(node for node in result["nodes"] if len(node["temporal"]["daily"]) >= 2)
    day = row["temporal"]["daily"][0]
    answer = execute_tool(
        result,
        "inspect_nodes",
        inspect_args([row["gid"]], detail="temporal", date_from=day["date"], date_to=day["date"]),
    )
    requested = answer["facts"][0]["requested_period"]
    assert requested["active_days"] == 1
    assert requested["in_kzt"] == day["in_kzt"]
    assert requested["out_tx"] == day["out_tx"]
    assert requested["daily"] == [day]
    assert answer["facts"][0]["full_period_summary"]["active_days"] == len(row["temporal"]["daily"])


@pytest.mark.parametrize(
    "changes",
    [
        {"date_from": "2026-09-01"},
        {"detail": "temporal", "date_from": "2026-09-10", "date_to": "2026-09-01"},
        {"detail": "temporal", "date_from": "20260901"},
        {"detail": "temporal", "date_from": "2026-02-30"},
    ],
)
def test_dates_are_not_silently_ignored_or_normalized(result, changes):
    with pytest.raises(DataError):
        execute_tool(result, "inspect_nodes", inspect_args([result["nodes"][0]["gid"]], **changes))


def test_temporal_large_period_reports_omitted_days_and_exact_totals(result):
    altered = deepcopy(result)
    row = altered["nodes"][0]
    row["temporal"]["daily"] = [
        {
            "date": f"2026-{month:02}-{day:02}",
            "in_kzt": 100,
            "out_kzt": 50,
            "in_tx": 1,
            "out_tx": 1,
            "senders": 1,
            "receivers": 1,
        }
        for month in (1, 2)
        for day in range(1, 21)
    ]
    answer = execute_tool(altered, "inspect_nodes", inspect_args([row["gid"]], detail="temporal"))
    requested = answer["facts"][0]["requested_period"]
    assert requested["in_kzt"] == 4_000
    assert len(requested["daily"]) == 31 and requested["omitted_days"] == 9


def test_community_gid_and_explicit_cluster_are_equivalent(result):
    node = result["nodes"][0]
    by_gid = execute_tool(
        result, "inspect_community", {"gid": node["gid"], "cluster_id": None, "limit": 3}
    )
    by_cluster = execute_tool(
        result, "inspect_community", {"gid": None, "cluster_id": node["cluster_id"], "limit": 3}
    )
    assert by_gid == by_cluster
    assert all(row["cluster_id"] == node["cluster_id"] for row in by_gid["nodes"])
    for gid, cluster in ((None, None), (node["gid"], node["cluster_id"]), (None, 99999)):
        with pytest.raises(DataError):
            execute_tool(
                result, "inspect_community", {"gid": gid, "cluster_id": cluster, "limit": 3}
            )


def test_directed_path_preserves_observed_edge_facts(result):
    edge = next(edge for edge in result["edges"] if edge["src"] != edge["dst"])
    answer = execute_tool(
        result,
        "trace_flows",
        {
            "operation": "path",
            "gids": [edge["src"], edge["dst"]],
            "max_hops": 1,
            "min_sources": 0,
            "limit": 10,
        },
    )
    assert answer["facts"][0]["paths"][0]["edges"] == [edge]
    assert len(answer["nodes"]) == 2


def test_removal_counts_edges_once_and_excludes_removed_pairs(result):
    altered = deepcopy(result)
    altered["nodes"] = altered["nodes"][:3]
    for node, gid in zip(altered["nodes"], ["1", "2", "3"], strict=True):
        node["gid"] = gid
    altered["edges"] = [
        {"src": "1", "dst": "2", "sum_kzt": 1000, "n_tx": 1},
        {"src": "2", "dst": "3", "sum_kzt": 1000, "n_tx": 1},
        {"src": "3", "dst": "3", "sum_kzt": 500, "n_tx": 1},
    ]
    fact = execute_tool(altered, "assess_removal", {"gids": ["2"]})["facts"][0]
    assert fact["removed_edges"] == 2 and fact["after_edges"] == 1
    assert fact["incident_turnover_kzt"] == 2000 and fact["incident_turnover_share"] == 0.8
    assert fact["before_components"] == 1 and fact["after_components"] == 2
    assert fact["fragmented_surviving_pairs_share"] == 1
    assert len(altered["edges"]) == 3


def test_read_only_all_outputs_bounded(result):
    original = deepcopy(result)
    gids = [node["gid"] for node in result["nodes"][:20]]
    calls = [
        ("find_nodes", find_args(limit=20)),
        ("inspect_nodes", inspect_args(gids, detail="role")),
        ("inspect_nodes", inspect_args(gids, detail="temporal")),
        ("assess_removal", {"gids": gids}),
    ]
    for name, args in calls:
        response = execute_tool(result, name, args)
        assert len(json.dumps(response, ensure_ascii=False)) <= MAX_OUTPUT_CHARS
        assert len(response["nodes"]) <= 20
        response["facts"].clear()
    assert result == original


def test_computational_limit_is_explicit_not_an_empty_success(result):
    altered = {**result, "edges": result["edges"] * 1100}
    assert len(altered["edges"]) > 100_000
    with pytest.raises(DataError, match="лимит"):
        execute_tool(altered, "assess_removal", {"gids": [result["nodes"][0]["gid"]]})
