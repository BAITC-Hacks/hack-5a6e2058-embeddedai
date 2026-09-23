"""Saved temporal evidence must agree with its daily aggregates and schema version."""

import json
from copy import deepcopy

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from money_graph import api, storage
from money_graph.loader import DataError
from money_graph.pipeline import RESULT_VERSION, analyze, validate_result, write_result


@pytest.fixture(scope="module")
def temporal_result(tmp_path_factory):
    source = tmp_path_factory.mktemp("temporal-contract")
    nodes = pd.DataFrame(
        {
            "gid": [0, 1, 2, 3, 4, 5],
            "is_seed": [True, True, True, False, False, True],
            "depth": [0, 0, 0, 1, 2, 0],
        }
    )
    transfers = []
    for day in range(1, 7):
        transfers.extend(
            [
                (0, 3, f"2026-07-{day:02}", 5000.0 if day <= 3 else 20_000.0),
                (3, 4, f"2026-07-{day:02}", 5000.0),
            ]
        )
    for sender in (0, 1, 2):
        transfers.extend([(sender, 3, "2026-07-07", 5000.0)] * 4)
    transfers.extend([(3, 4, "2026-07-07", 5000.0)] * 8)
    tx = pd.DataFrame(transfers, columns=["src", "dst", "date", "sum_kzt"])
    edges = tx.groupby(["src", "dst"], as_index=False).agg(
        sum_kzt=("sum_kzt", "sum"), n_tx=("sum_kzt", "size")
    )
    edges["depth"] = edges.src.map({0: 1, 1: 1, 2: 1, 3: 2})
    for name, frame in (("nodes", nodes), ("edges", edges), ("transactions", tx)):
        frame.to_parquet(source / f"{name}.parquet", index=False)
    return analyze(source)


def hub(result):
    return next(row for row in result["nodes"] if row["gid"] == "3")


def legacy(result):
    saved = deepcopy(result)
    for key in (
        "result_version",
        "temporal_patterns_version",
        "n_activity_spike_nodes",
        "n_synchronous_nodes",
        "n_repeated_amount_nodes",
    ):
        saved["report"].pop(key, None)
    for row in saved["nodes"]:
        row["temporal"].pop("patterns")
    return saved


def test_current_temporal_snapshot_roundtrips_without_changing_csv(temporal_result, tmp_path):
    validate_result(temporal_result)
    patterns = hub(temporal_result)["temporal"]["patterns"]
    assert temporal_result["report"]["result_version"] == RESULT_VERSION
    assert patterns["activity"]["spike_day_count"] == 1
    assert patterns["synchronous"]["day_count"] == 1
    assert patterns["repeated_amounts"]["group_count"] == 2
    run = "a" * 32
    write_result(temporal_result, tmp_path / run)
    loaded = storage.read_result(tmp_path, run, temporal_result["report"]["rules"])
    assert loaded == temporal_result


@pytest.mark.parametrize(
    "path,value",
    [
        (("version",), "wrong"),
        (("caveat",), ""),
        (("activity",), []),
        (("activity", "status"), "invalid"),
        (("activity", "status"), "insufficient_history"),
        (("activity", "active_days"), -1),
        (("activity", "minimum_days"), 1),
        (("activity", "baseline_median_tx"), float("nan")),
        (("activity", "baseline_median_tx"), 3.0),
        (("activity", "threshold_tx"), None),
        (("activity", "threshold_tx"), 8.0),
        (("activity", "spike_day_count"), 999),
        (("activity", "spike_days"), []),
        (("activity", "spike_days"), [None]),
        (("activity", "spike_days", 0, "date"), "2026-07-08"),
        (("activity", "spike_days", 0, "n_tx"), 999),
        (("activity", "spike_days", 0, "in_tx"), 999),
        (("activity", "spike_days", 0, "in_kzt"), -1),
        (("activity", "spike_days", 0, "in_kzt"), 60_001.0),
        (("synchronous",), None),
        (("synchronous", "minimum_senders"), True),
        (("synchronous", "day_count"), 999),
        (("synchronous", "days"), []),
        (("synchronous", "days", 0, "date"), "not-date"),
        (("synchronous", "days", 0, "senders"), 99),
        (("synchronous", "days", 0, "in_tx"), 0),
        (("synchronous", "days", 0, "in_kzt"), 60_001.0),
        (("repeated_amounts",), []),
        (("repeated_amounts", "group_count"), 999),
        (("repeated_amounts", "groups"), []),
        (("repeated_amounts", "minimum_transactions"), 1),
        (("repeated_amounts", "directions"), []),
        (("repeated_amounts", "directions", "in", "n_transactions"), 999),
        (("repeated_amounts", "directions", "in", "status"), "insufficient_transactions"),
        (("repeated_amounts", "directions", "in", "q1_kzt"), float("nan")),
        (("repeated_amounts", "groups", 0, "date"), "2026-07-01"),
        (("repeated_amounts", "groups", 0, "direction"), "bad"),
        (("repeated_amounts", "groups", 0, "n_tx"), 999),
        (("repeated_amounts", "groups", 0, "counterparties"), 99),
        (("repeated_amounts", "groups", 0, "total_kzt"), 60_001.0),
        (("repeated_amounts", "groups", 0, "amount_kzt"), 0),
    ],
)
def test_corrupt_temporal_pattern_shapes_and_facts_are_rejected(temporal_result, path, value):
    saved = deepcopy(temporal_result)
    target = hub(saved)["temporal"]["patterns"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(DataError):
        validate_result(saved)


def test_duplicate_temporal_examples_cannot_inflate_signal_counts(temporal_result):
    for category, examples in (
        ("activity", "spike_days"),
        ("synchronous", "days"),
        ("repeated_amounts", "groups"),
    ):
        saved = deepcopy(temporal_result)
        items = hub(saved)["temporal"]["patterns"][category][examples]
        items.append(deepcopy(items[0]))
        with pytest.raises(DataError):
            validate_result(saved)


@pytest.mark.parametrize(
    "key,value",
    [
        ("temporal_patterns_version", "2"),
        ("n_activity_spike_nodes", 999),
        ("n_synchronous_nodes", -1),
        ("n_repeated_amount_nodes", True),
        ("result_version", 2.0),
        ("result_version", None),
    ],
)
def test_report_temporal_aggregates_and_versions_cannot_misrepresent_nodes(
    temporal_result, key, value
):
    saved = deepcopy(temporal_result)
    saved["report"][key] = value
    with pytest.raises(DataError):
        validate_result(saved)


def test_isolate_and_short_history_cannot_claim_assessed_patterns(temporal_result):
    saved = deepcopy(temporal_result)
    isolate = next(row for row in saved["nodes"] if row["gid"] == "5")
    isolate["temporal"]["patterns"]["activity"]["status"] = "assessed"
    with pytest.raises(DataError):
        validate_result(saved)
    saved = deepcopy(temporal_result)
    short = next(row for row in saved["nodes"] if row["gid"] == "1")
    short["temporal"]["patterns"]["repeated_amounts"]["directions"]["out"]["q1_kzt"] = 5000.0
    with pytest.raises(DataError):
        validate_result(saved)


def test_legacy_snapshot_can_be_read_for_recovery_but_not_reused_as_current(
    temporal_result, tmp_path
):
    saved = legacy(temporal_result)
    validate_result(saved)
    run = "b" * 32
    (tmp_path / run).mkdir()
    (tmp_path / run / "result.json").write_text(json.dumps(saved), encoding="utf-8")
    assert storage.read_result(tmp_path, run) == saved
    with pytest.raises(storage.ObsoleteRun):
        storage.read_result(tmp_path, run, saved["report"]["rules"])


def test_current_snapshot_missing_patterns_is_corrupt_not_silently_legacy(
    temporal_result, tmp_path
):
    saved = deepcopy(temporal_result)
    hub(saved)["temporal"].pop("patterns")
    run = "c" * 32
    (tmp_path / run).mkdir()
    (tmp_path / run / "result.json").write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(storage.CorruptRun):
        storage.read_result(tmp_path, run, saved["report"]["rules"])


def test_bootstrap_schema_migration_recomputes_and_preserves_old_csv(tmp_path):
    client = TestClient(api.create_app(storage=tmp_path))
    old = client.get("/api/bootstrap").json()["run_id"]
    path = tmp_path / old / "result.json"
    saved = legacy(json.loads(path.read_text(encoding="utf-8")))
    path.write_text(json.dumps(saved), encoding="utf-8")
    previous_csv = (tmp_path / old / "nodes_roles.csv").read_bytes()
    restarted = TestClient(api.create_app(storage=tmp_path))
    new = restarted.get("/api/bootstrap").json()["run_id"]
    assert new != old
    assert restarted.get(f"/api/runs/{new}").json()["result_version"] == RESULT_VERSION
    assert restarted.get(f"/api/runs/{old}").status_code == 409
    assert restarted.get(f"/api/runs/{old}/exports/nodes_roles.csv").content == previous_csv
