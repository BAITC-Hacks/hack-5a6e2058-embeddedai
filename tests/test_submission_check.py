"""Independent file checks detect corrupt artifacts, stale reports and raw mismatches."""

import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from money_graph.demo import create_demo
from money_graph.exports import EXPORT_SCHEMAS
from money_graph.pipeline import analyze, write_result

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_submission.py"
spec = importlib.util.spec_from_file_location("submission_check", SCRIPT)
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    base = tmp_path_factory.mktemp("submission-baseline")
    source, output = base / "input", base / "output"
    create_demo(source)
    result = analyze(source)
    write_result(result, output)
    return source, output


@pytest.fixture
def submission(tmp_path, baseline):
    source, output = tmp_path / "input", tmp_path / "output"
    shutil.copytree(baseline[0], source)
    shutil.copytree(baseline[1], output)
    return source, output


def table(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def write_table(path, rows, columns=None):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=columns or checker.SCHEMAS[path.name], lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def only_csv(output):
    for filename in ("result.json", "run_report.json"):
        (output / filename).unlink(missing_ok=True)


def test_independent_public_contract_matches_serializer_and_validates_full_generation(submission):
    source, output = submission
    assert checker.SCHEMAS == EXPORT_SCHEMAS
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    actual = checker.check_submission(output, source)
    assert actual["status"] == "success"
    expected = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    assert actual["counts"] == {
        "nodes": expected["n_nodes"],
        "clusters": expected["n_clusters"],
        "top_nodes": min(50, expected["n_nodes"]),
    }
    assert all(actual["checks"].values())
    assert actual["raw"]["n_edges"] == 95
    assert actual["raw"]["internal_turnover_kzt"] + actual["raw"][
        "intercluster_turnover_kzt"
    ] == pytest.approx(actual["raw"]["turnover_kzt"], abs=0.01)
    assert all(len(item["sha256"]) == 64 for item in actual["exports"].values())
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}


def test_csv_only_script_runs_with_no_packages_network_key_node_or_uv(submission, tmp_path):
    _, output = submission
    only_csv(output)
    process = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--out", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": "", "PYTHONUTF8": "1"},
    )
    assert process.returncode == 0, process.stderr
    actual = json.loads(process.stdout)
    assert actual["scope"] == "csv_only"
    assert actual["checks"]["raw_node_coverage_and_cluster_totals"] is False
    assert actual["checks"]["report_consistency"] is False
    assert "Traceback" not in process.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("gid", "01"),
        ("gid", "9223372036854775808"),
        ("gid", "9.007199254741001e15"),
        ("gid", " 1"),
        ("role", "organizer"),
        ("role_score", "NaN"),
        ("role_score", "inf"),
        ("priority_score", "-0.1"),
        ("priority_score", "1.001"),
        ("role_score", ""),
        ("evidence", " "),
        ("evidence", "я" * 201),
        ("cluster_id", "1.5"),
    ],
)
def test_invalid_nodes_fail_with_actionable_file_diagnostic(submission, field, value):
    _, output = submission
    only_csv(output)
    path = output / "nodes_roles.csv"
    rows = table(path)
    rows[0][field] = value
    write_table(path, rows)
    with pytest.raises(checker.SubmissionError, match="nodes_roles.csv"):
        checker.check_submission(output)


@pytest.mark.parametrize("filename", list(EXPORT_SCHEMAS))
def test_missing_extra_or_duplicate_columns_cannot_pass_contract(submission, filename):
    _, output = submission
    path = output / filename
    rows = table(path)
    original = path.read_bytes()
    for columns in (
        (*EXPORT_SCHEMAS[filename], "unexpected"),
        (*EXPORT_SCHEMAS[filename], EXPORT_SCHEMAS[filename][0]),
    ):
        write_table(path, rows, columns)
        with pytest.raises(checker.SubmissionError, match="схема"):
            checker.check_submission(output)
    path.write_bytes(original)
    path.unlink()
    with pytest.raises(checker.SubmissionError, match="Отсутствует"):
        checker.check_submission(output)


def test_duplicate_gid_is_not_mistaken_for_complete_coverage(submission):
    _, output = submission
    path = output / "nodes_roles.csv"
    rows = table(path)
    rows[1]["gid"] = rows[0]["gid"]
    write_table(path, rows)
    with pytest.raises(checker.SubmissionError, match="повтор gid"):
        checker.check_submission(output)


@pytest.mark.parametrize(
    "field,value",
    [
        ("n_nodes", "999"),
        ("n_seed", "999"),
        ("sum_kzt_internal", "NaN"),
        ("sum_kzt_internal", "-1"),
        ("hypothesis", " "),
        ("top_gids", "[]"),
        ("top_gids", "[123]"),
        ("top_gids", '["999999"]'),
        ("top_gids", "not-json"),
    ],
)
def test_cluster_membership_stats_and_explanations_are_checked(submission, field, value):
    _, output = submission
    only_csv(output)
    path = output / "clusters.csv"
    rows = table(path)
    rows[0][field] = value
    write_table(path, rows)
    with pytest.raises(checker.SubmissionError, match="clusters.csv"):
        checker.check_submission(output)


@pytest.mark.parametrize("change", ["role", "score", "rank", "why", "omit", "reorder"])
def test_top_is_exact_global_prefix_with_same_roles_and_scores(submission, change):
    _, output = submission
    only_csv(output)
    path = output / "top_nodes.csv"
    rows = table(path)
    if change == "role":
        rows[0]["role"] = "unknown"
    elif change == "score":
        rows[0]["priority_score"] = "0"
    elif change == "rank":
        rows[0]["rank"] = "2"
    elif change == "why":
        rows[0]["why"] = ""
    elif change == "omit":
        rows = rows[:19]
    else:
        rows[:2] = reversed(rows[:2])
    write_table(path, rows)
    with pytest.raises(checker.SubmissionError, match="top_nodes.csv"):
        checker.check_submission(output)


@pytest.mark.parametrize("field", ["n_seed", "sum_kzt_internal"])
def test_raw_checks_detect_plausible_but_wrong_cluster_aggregates(submission, field):
    source, output = submission
    only_csv(output)
    path = output / "clusters.csv"
    rows = table(path)
    selected = next(row for row in rows if int(row["n_seed"]) > 0)
    selected[field] = (
        str(int(selected[field]) - 1) if field == "n_seed" else str(float(selected[field]) + 0.02)
    )
    write_table(path, rows)
    assert checker.check_submission(output)["status"] == "success"
    with pytest.raises(checker.SubmissionError, match="clusters.csv"):
        checker.check_submission(output, source)


def test_source_provenance_detects_stale_report_even_when_values_are_identical(submission):
    source, output = submission
    path = source / "nodes.parquet"
    frame = pd.read_parquet(path)
    frame.to_parquet(path, index=False, compression="gzip")
    with pytest.raises(checker.SubmissionError, match="SHA-256"):
        checker.check_submission(output, source)


def test_snapshot_and_csv_cannot_be_from_different_generations(submission):
    _, output = submission
    path = output / "result.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["nodes"][0]["evidence"] = "Устаревшее пояснение"
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(checker.SubmissionError, match="result.json.*evidence"):
        checker.check_submission(output)


def test_report_counts_cannot_be_stale_or_hide_nan(submission):
    _, output = submission
    path = output / "run_report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["n_nodes"] += 1
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(checker.SubmissionError, match="n_nodes"):
        checker.check_submission(output)
    path.write_text('{"n_nodes": NaN}', encoding="utf-8")
    with pytest.raises(checker.SubmissionError, match="JSON"):
        checker.check_submission(output)


def test_optional_raw_input_errors_are_nonzero_json_without_traceback(submission, tmp_path):
    source, output = submission
    process = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--out", str(output), "--data", str(source)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ | {"PYTHONUTF8": "1"},
    )
    assert process.returncode == 2
    assert json.loads(process.stdout)["status"] == "error"
    assert "uv run --frozen --no-dev" in json.loads(process.stdout)["error"]
    assert "Traceback" not in process.stderr


def test_arbitrary_signed_ids_and_small_graph_self_transfers_match_raw_turnover(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir()
    a, b, isolated = -(2**63), 2**63 - 1, 2**53 + 1
    nodes = pd.DataFrame(
        {"gid": [a, b, isolated], "depth": [0, 1, 0], "is_seed": [True, False, True]}
    )
    edges = pd.DataFrame(
        {
            "src": [a, b],
            "dst": [b, b],
            "sum_kzt": [5000.0, 10_000.0],
            "n_tx": [1, 1],
            "depth": [1, 2],
        }
    )
    transactions = edges[["src", "dst", "sum_kzt"]].copy()
    transactions["date"] = "2026-07-01"
    for name, frame in (("nodes", nodes), ("edges", edges), ("transactions", transactions)):
        frame.to_parquet(source / f"{name}.parquet", index=False)
    write_result(analyze(source), output)
    result = checker.check_submission(output, source)
    assert result["counts"]["nodes"] == result["counts"]["top_nodes"] == 3
    assert result["raw"]["self_transfer_kzt_included_in_internal"] == 10_000
    assert result["raw"]["turnover_kzt"] == 15_000


def test_raw_node_coverage_and_transaction_edge_consistency_are_independent(submission):
    source, output = submission
    only_csv(output)
    extra = pd.read_parquet(source / "nodes.parquet")
    extra.loc[len(extra)] = [-(2**63), 0, True]
    extra.to_parquet(source / "nodes.parquet", index=False)
    with pytest.raises(checker.SubmissionError, match="набор gid"):
        checker.check_submission(output, source)
    extra.iloc[:-1].to_parquet(source / "nodes.parquet", index=False)
    tx = pd.read_parquet(source / "transactions.parquet")
    tx.loc[0, "sum_kzt"] += 1000
    tx.to_parquet(source / "transactions.parquet", index=False)
    with pytest.raises(ValueError, match="сумм|суммами|sum|Сумм"):
        checker.check_submission(output, source)


def test_equal_priorities_use_numeric_gid_order_not_csv_order_or_lexicographic_order(tmp_path):
    nodes = [
        {
            "gid": gid,
            "role": "peripheral",
            "role_score": "0",
            "cluster_id": "0",
            "priority_score": "0.5",
            "evidence": "Проверяемая гипотеза",
        }
        for gid in ("10", "2", "-1")
    ]
    cluster = {
        "cluster_id": "0",
        "n_nodes": "3",
        "n_seed": "0",
        "sum_kzt_internal": "0",
        "top_gids": '["-1","2","10"]',
        "hypothesis": "Структурная группа",
    }
    top = [
        {
            "rank": str(index),
            "gid": gid,
            "role": "peripheral",
            "priority_score": "0.5",
            "why": "Одинаковый приоритет; числовой порядок gid",
        }
        for index, gid in enumerate(("-1", "2", "10"), 1)
    ]
    write_table(tmp_path / "nodes_roles.csv", nodes)
    write_table(tmp_path / "clusters.csv", [cluster])
    write_table(tmp_path / "top_nodes.csv", top)
    assert checker.check_submission(tmp_path)["counts"]["top_nodes"] == 3
    top[1]["gid"], top[2]["gid"] = top[2]["gid"], top[1]["gid"]
    write_table(tmp_path / "top_nodes.csv", top)
    with pytest.raises(checker.SubmissionError, match="числовой порядок"):
        checker.check_submission(tmp_path)
