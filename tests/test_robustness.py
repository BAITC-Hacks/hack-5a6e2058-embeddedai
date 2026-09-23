"""Partition invariants and falsifiable role-threshold counterexamples."""

import json
from copy import deepcopy
from itertools import product

import pytest

from money_graph import robustness
from money_graph.demo import create_demo
from money_graph.loader import DataError
from money_graph.pipeline import analyze, read_rules
from money_graph.roles import classify


@pytest.fixture(scope="module")
def analyzed(tmp_path_factory):
    source = tmp_path_factory.mktemp("robustness")
    create_demo(source)
    return analyze(source)


def role_node(**overrides):
    row = {
        "gid": "9007199254740993",
        "rank": 1,
        "pass_through": 0.2,
        "is_seed": False,
        "boundary_censored": False,
        "isolated": False,
        "seed_reach": 0,
        "in_deg": 3,
        "out_deg": 1,
        "p_betweenness": 0.0,
        "p_in_tx": 0.5,
        "p_out_tx": 0.5,
        "p_out_kzt": 0.5,
        "in_tx": 3,
        "out_tx": 1,
        "in_kzt": 100_000.0,
        "out_kzt": 20_000.0,
        "observed_out_exceeds_in": False,
    } | overrides
    row.update(classify(row, read_rules()))
    return row


def test_adjusted_rand_ignores_labels_and_handles_extreme_partitions():
    assert robustness.adjusted_rand([0, 0, 1, 1], [8, 8, 4, 4]) == 1
    assert robustness.adjusted_rand([0, 0, 1, 1], [0, 1, 0, 1]) == -0.5
    assert robustness.adjusted_rand([0, 0, 1, 1], [0, 0, 0, 0]) == 0
    assert robustness.adjusted_rand([0, 1, 2], [0, 0, 0]) == 0
    assert robustness.adjusted_rand([0, 1, 2], [9, 8, 7]) == 1
    assert robustness.adjusted_rand([0], [1]) == 1
    assert robustness.adjusted_rand([], []) == 1
    with pytest.raises(ValueError, match="same nodes"):
        robustness.adjusted_rand([1], [])


def test_adjusted_rand_matches_independent_pair_count_formula_for_small_partitions():
    # Exhaustively check all binary partitions of four nodes against direct
    # pair comparisons, instead of copying the contingency implementation.
    for left in product(range(2), repeat=4):
        for right in product(range(2), repeat=4):
            both = left_same = right_same = 0
            for i in range(4):
                for j in range(i):
                    a, b = left[i] == left[j], right[i] == right[j]
                    left_same += a
                    right_same += b
                    both += a and b
            expected = left_same * right_same / 6
            maximum = (left_same + right_same) / 2
            value = (both - expected) / (maximum - expected) if maximum != expected else 1
            assert robustness.adjusted_rand(left, right) == pytest.approx(value)
            assert robustness.adjusted_rand(left, right) == robustness.adjusted_rand(right, left)


def test_role_threshold_variants_are_explicit_bounded_and_do_not_change_rules():
    rules = read_rules()
    original = deepcopy(rules)
    scenarios = robustness.role_scenarios(rules)
    assert rules == original
    assert len(scenarios) == 16
    assert len({item["id"] for item in scenarios}) == 16
    changes = [item["changes"] for item in scenarios]
    assert {"distributor_min_out": 8} in changes
    assert {"distributor_min_out": 12} in changes
    assert {"coordinator_betweenness_percentile": 0.85} in changes
    assert {"coordinator_betweenness_percentile": 0.95} in changes
    for item in scenarios:
        assert 1 <= len(item["changes"]) <= 2
        if len(item["changes"]) == 2:
            assert set(item["changes"]) == {"transit_ratio_min", "transit_ratio_max"}
        assert all(key != "priority_weights" for key in item["changes"])
        assert any(original[key] != value for key, value in item["changes"].items())


def test_borderline_roles_have_traceable_alternatives_without_seed_boundary_claims():
    rules = read_rules()
    nodes = [
        role_node(),
        role_node(gid="2", rank=2, is_seed=True),
        role_node(gid="3", rank=3, boundary_censored=True),
        role_node(gid="4", rank=4, pass_through=1.16, out_kzt=116_000),
        role_node(gid="5", rank=5, in_deg=4, out_deg=10, out_tx=10),
    ]
    result = robustness._roles(nodes, rules)
    borderline = result["nodes"][0]
    assert borderline["gid"] == "9007199254740993"
    assert borderline["baseline_role"] == "consolidator"
    assert borderline["unchanged_fraction"] == 14 / 16
    assert set(borderline["changed_scenarios"]) == {
        "consolidator_min_in_upper",
        "consolidator_ratio_max_lower",
    }
    assert {item["role"] for item in borderline["alternatives"]} == {"consolidator", "peripheral"}
    assert result["nodes"][1]["unchanged_fraction"] == 1
    assert result["nodes"][2]["unchanged_fraction"] == 1
    assert "transit_band_narrow" in result["nodes"][3]["changed_scenarios"]
    assert "distributor_min_out_upper" in result["nodes"][4]["changed_scenarios"]
    assert all(
        sum(t["count"] for t in s["transitions"]) == s["changed_nodes"] for s in result["scenarios"]
    )


def test_complete_diagnostic_is_repeatable_json_safe_and_preserves_main_result(analyzed):
    before = deepcopy(analyzed)
    first = robustness.analyze_robustness(analyzed)
    second = robustness.analyze_robustness(analyzed)
    assert first == second
    assert analyzed == before
    json.dumps(first, allow_nan=False)
    communities = first["communities"]
    assert communities["status"] == "complete"
    assert communities["scenario_count"] == 8
    assert communities["n_active_nodes"] == analyzed["report"]["n_active_nodes"]
    assert communities["excluded_isolates"] == analyzed["report"]["n_isolates"]
    assert {s["kind"] for s in communities["scenarios"]} == {
        "seed",
        "resolution",
        "resolution_and_seed",
    }
    baseline = (communities["baseline_seed"], communities["baseline_resolution"])
    assert baseline not in {(s["seed"], s["resolution"]) for s in communities["scenarios"]}
    assert len(first["roles"]["top20"]) == len(communities["top20"]) == 20
    assert [row["gid"] for row in first["roles"]["top20"]] == [
        row["gid"] for row in analyzed["top"][:20]
    ]


def test_isolates_cannot_inflate_headline_agreement(analyzed):
    extended = deepcopy(analyzed)
    empty = next(row for row in extended["nodes"] if row["isolated"])
    for index in range(100):
        extended["nodes"].append(
            dict(empty, gid=str(-index - 1), rank=1000 + index, cluster_id=1000 + index)
        )
    original = robustness.analyze_robustness(analyzed)
    actual = robustness.analyze_robustness(extended)
    assert (
        actual["roles"]["minimum_unchanged_fraction"]
        == original["roles"]["minimum_unchanged_fraction"]
    )
    assert actual["communities"]["scenarios"] == original["communities"]["scenarios"]
    assert (
        actual["communities"]["excluded_isolates"]
        == original["communities"]["excluded_isolates"] + 100
    )
    assert all(
        row["unchanged_fraction"] is None and not row["assessed"]
        for row in actual["roles"]["nodes"][-100:]
    )


def test_no_edges_has_no_spurious_perfect_stability(analyzed):
    empty = deepcopy(next(row for row in analyzed["nodes"] if row["isolated"]))
    result = {"nodes": [empty], "edges": [], "report": analyzed["report"]}
    checked = robustness.analyze_robustness(result)
    assert checked["roles"]["minimum_unchanged_fraction"] is None
    assert checked["communities"]["status"] == "not_applicable"
    assert checked["communities"]["minimum_adjusted_rand"] is None
    assert checked["communities"]["top20"][0]["mean_membership_jaccard"] is None


def test_budget_reports_partial_trials_instead_of_claiming_full_agreement(analyzed, monkeypatch):
    monkeypatch.setattr(robustness, "COMMUNITY_SECONDS", 0)
    result = robustness.analyze_robustness(analyzed)
    assert result["communities"]["status"] == "limited"
    assert result["communities"]["scenario_count"] == 0
    assert result["communities"]["planned_scenario_count"] == 8
    assert result["communities"]["minimum_adjusted_rand"] is None
    assert result["communities"]["warnings"]
    assert result["roles"]["scenario_count"] == 16


def test_completed_trials_survive_the_between_scenario_time_budget(analyzed, monkeypatch):
    ticks = iter([0.0, 0.0, 21.0])
    monkeypatch.setattr(robustness.time, "monotonic", lambda: next(ticks))
    result = robustness.analyze_robustness(analyzed)["communities"]
    assert result["status"] == "limited"
    assert result["scenario_count"] == 1
    assert result["minimum_adjusted_rand"] == result["scenarios"][0]["adjusted_rand"]
    assert any(row["assessed"] for row in result["top20"])


def test_changed_saved_rules_and_oversized_diagnostic_are_rejected(analyzed, monkeypatch):
    wrong = deepcopy(analyzed)
    wrong["nodes"][0]["role"] = "invalid"
    with pytest.raises(DataError, match="не соответствуют"):
        robustness.analyze_robustness(wrong)
    monkeypatch.setattr(robustness, "MAX_NODES", 1)
    with pytest.raises(DataError, match="10 000"):
        robustness.analyze_robustness(analyzed)


def test_singleton_communities_compare_peers_without_self_inflation(analyzed, monkeypatch):
    # Put every active node alone. A singleton result should not gain agreement
    # from its focal node when the original community had other members.
    def singleton(graph, **kwargs):
        return [{gid} for gid in graph]

    monkeypatch.setattr(robustness.nx.community, "louvain_communities", singleton)
    result = robustness.analyze_robustness(analyzed)
    cluster_sizes = {cluster["cluster_id"]: cluster["n_nodes"] for cluster in analyzed["clusters"]}
    for row in result["communities"]["top20"]:
        if row["assessed"]:
            assert row["mean_membership_jaccard"] == int(
                cluster_sizes[row["baseline_cluster_id"]] == 1
            )


def test_community_membership_is_unchanged_when_cluster_labels_are_permuted(analyzed, monkeypatch):
    def reversed_baseline(graph, **kwargs):
        groups = {}
        for row in analyzed["nodes"]:
            gid = int(row["gid"])
            if gid in graph:
                groups.setdefault(row["cluster_id"], set()).add(gid)
        return list(reversed(list(groups.values())))

    monkeypatch.setattr(robustness.nx.community, "louvain_communities", reversed_baseline)
    communities = robustness.analyze_robustness(analyzed)["communities"]
    assert communities["minimum_adjusted_rand"] == 1
    assert all(
        row["minimum_membership_jaccard"] == 1 for row in communities["top20"] if row["assessed"]
    )
