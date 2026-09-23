import os
import shutil
import time
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from money_graph import api
from money_graph.demo import create_demo


def files_at(source: Path):
    return {
        name: (
            f"{name}.parquet",
            (source / f"{name}.parquet").read_bytes(),
            "application/octet-stream",
        )
        for name in ("nodes", "edges", "transactions")
    }


def test_default_mode_does_not_republish_previous_explicit_dataset(tmp_path):
    source, storage = tmp_path / "input", tmp_path / "runs"
    create_demo(source)
    first = TestClient(api.create_app(initial_data=source, storage=storage))
    explicit = first.get("/api/bootstrap").json()
    assert not explicit["synthetic"]
    restarted = TestClient(api.create_app(storage=storage))
    default = restarted.get("/api/bootstrap").json()
    assert default["synthetic"] and default["run_id"] != explicit["run_id"]
    assert restarted.get(f"/api/runs/{explicit['run_id']}").status_code == 200


def test_explicit_bootstrap_reuses_unchanged_input_and_recalculates_changed_input(tmp_path):
    source, storage = tmp_path / "input", tmp_path / "runs"
    create_demo(source)
    first = (
        TestClient(api.create_app(initial_data=source, storage=storage))
        .get("/api/bootstrap")
        .json()
    )
    same = (
        TestClient(api.create_app(initial_data=source, storage=storage))
        .get("/api/bootstrap")
        .json()
    )
    assert first == same
    # A harmless new nullable metadata field changes source fingerprint; loader ignores it.
    path = source / "nodes.parquet"
    nodes = pd.read_parquet(path)
    nodes["source_note"] = "updated source"
    nodes.to_parquet(path, index=False)
    changed = (
        TestClient(api.create_app(initial_data=source, storage=storage))
        .get("/api/bootstrap")
        .json()
    )
    assert changed["run_id"] != first["run_id"]


def test_corrupt_bootstrap_or_result_recovers_without_removing_exports(tmp_path):
    client = TestClient(api.create_app(storage=tmp_path))
    run = client.get("/api/bootstrap").json()["run_id"]
    csv = (tmp_path / run / "nodes_roles.csv").read_bytes()
    (tmp_path / "bootstrap.json").write_text("{broken")
    recovered = TestClient(api.create_app(storage=tmp_path))
    next_run = recovered.get("/api/bootstrap").json()["run_id"]
    assert next_run != run
    assert (tmp_path / run / "nodes_roles.csv").read_bytes() == csv
    (tmp_path / next_run / "result.json").write_text("{broken")
    assert recovered.get(f"/api/runs/{next_run}").status_code == 503
    assert recovered.get(f"/api/runs/{next_run}/exports/nodes_roles.csv").status_code == 200
    assert (
        TestClient(api.create_app(storage=tmp_path)).get("/api/bootstrap").json()["run_id"]
        != next_run
    )


def test_rules_change_recalculates_bootstrap_but_old_csv_remains_downloadable(
    tmp_path, monkeypatch
):
    client = TestClient(api.create_app(storage=tmp_path))
    old = client.get("/api/bootstrap").json()["run_id"]
    rules = api.read_rules()
    # Same version, different actual configuration must not silently reuse cached output.
    rules["random_seed"] += 1
    monkeypatch.setattr(api, "read_rules", lambda: rules)
    # Bootstrap analyze uses the configuration reader in pipeline too.
    monkeypatch.setattr("money_graph.pipeline.read_rules", lambda path=None: rules)
    restarted = TestClient(api.create_app(storage=tmp_path))
    assert restarted.get("/api/bootstrap").json()["run_id"] != old
    assert restarted.get(f"/api/runs/{old}").status_code == 409
    exported = restarted.get(f"/api/runs/{old}/exports/nodes_roles.csv")
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "no-store"
    assert exported.headers["referrer-policy"] == "no-referrer"
    assert restarted.get(f"/api/runs/{old}/exports/result.json").status_code == 404


def test_failed_upload_does_not_prune_previous_runs_and_success_bounds_retention(tmp_path):
    source, storage = tmp_path / "input", tmp_path / "runs"
    create_demo(source)
    client = TestClient(api.create_app(storage=storage))
    bootstrap = client.get("/api/bootstrap").json()["run_id"]
    old_ids = []
    for i in range(20):
        run = f"{i:032x}"
        old_ids.append(run)
        shutil.copytree(storage / bootstrap, storage / run)
        timestamp = time.time() - 100 + i
        os.utime(storage / run, (timestamp, timestamp))
    files = files_at(source)
    broken = files | {"edges": ("edges.parquet", b"not parquet", "application/octet-stream")}
    assert client.post("/api/analyze", files=broken).status_code == 422
    assert all((storage / run).is_dir() for run in old_ids)
    result = client.post("/api/analyze", files=files)
    assert result.status_code == 200
    assert not (storage / old_ids[0]).exists()
    assert (storage / result.json()["run_id"]).is_dir()
    assert len([p for p in storage.iterdir() if p.is_dir()]) == 21
    assert (storage / bootstrap).is_dir()


def test_export_does_not_require_result_json_for_recovery(tmp_path):
    client = TestClient(api.create_app(storage=tmp_path))
    run = client.get("/api/bootstrap").json()["run_id"]
    (tmp_path / run / "result.json").unlink()
    assert client.get(f"/api/runs/{run}").status_code == 404
    assert client.get(f"/api/runs/{run}/exports/nodes_roles.csv").status_code == 200
    assert client.get("/api/runs/not-a-run/exports/nodes_roles.csv").status_code == 404


def test_synthetic_provenance_belongs_to_run_not_current_bootstrap(tmp_path):
    source, storage = tmp_path / "input", tmp_path / "runs"
    create_demo(source)
    demo = TestClient(api.create_app(storage=storage)).get("/api/bootstrap").json()["run_id"]
    explicit = TestClient(api.create_app(initial_data=source, storage=storage))
    assert explicit.get(f"/api/runs/{demo}").json()["synthetic"] is True
    selected = explicit.get("/api/bootstrap").json()["run_id"]
    assert explicit.get(f"/api/runs/{selected}").json()["synthetic"] is False


def test_valid_json_with_corrupt_nodes_is_not_reused(tmp_path):
    import json

    client = TestClient(api.create_app(storage=tmp_path))
    run = client.get("/api/bootstrap").json()["run_id"]
    path = tmp_path / run / "result.json"
    payload = json.loads(path.read_text())
    payload["nodes"] = [None]
    path.write_text(json.dumps(payload))
    assert client.get(f"/api/runs/{run}").status_code == 503
    assert client.get(f"/api/runs/{run}/graph").status_code == 503
    assert client.get(f"/api/runs/{run}/exports/nodes_roles.csv").status_code == 200
    restarted = TestClient(api.create_app(storage=tmp_path))
    assert restarted.get("/api/bootstrap").json()["run_id"] != run


def test_process_configuration_is_frozen_for_uploads(tmp_path, monkeypatch):
    source, storage = tmp_path / "input", tmp_path / "runs"
    create_demo(source)
    client = TestClient(api.create_app(storage=storage))
    old = client.get("/api/bootstrap").json()["run_id"]
    baseline = client.get(f"/api/runs/{old}").json()["rules"]
    changed = api.read_rules() | {"random_seed": baseline["random_seed"] + 1}
    original = api.read_rules

    def read_modified_default(path=None):
        return changed if path is None else original(path)

    monkeypatch.setattr("money_graph.pipeline.read_rules", read_modified_default)
    response = client.post("/api/analyze", files=files_at(source))
    assert response.status_code == 200
    run = response.json()["run_id"]
    saved = client.get(f"/api/runs/{run}")
    assert saved.status_code == 200
    assert saved.json()["rules"] == baseline
