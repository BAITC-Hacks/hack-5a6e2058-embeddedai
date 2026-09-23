from pathlib import Path

from fastapi.testclient import TestClient

from money_graph.api import create_app
from money_graph.demo import create_demo


def test_upload_search_download_and_restart(tmp_path):
    storage = tmp_path / "runs"
    client = TestClient(create_app(storage=storage))
    source = tmp_path / "input"
    create_demo(source)
    files = {
        name: (
            f"{name}.parquet",
            (source / f"{name}.parquet").read_bytes(),
            "application/octet-stream",
        )
        for name in ("nodes", "edges", "transactions")
    }
    response = client.post("/api/analyze", files=files)
    assert response.status_code == 200, response.text
    run = response.json()["run_id"]
    gid = "9007199254741007"  # An isolated seed with a JS-unsafe identifier.
    node = client.get(f"/api/runs/{run}/nodes/{gid}").json()
    assert node["gid"] == gid and node["isolated"]
    graph = client.get(f"/api/runs/{run}/graph", params={"gid": gid, "role": "terminal"}).json()
    assert [r["gid"] for r in graph["nodes"]] == [gid]
    assert graph["edges"] == []
    assert client.get(f"/api/runs/{run}/nodes/123456789").status_code == 404
    csv = client.get(f"/api/runs/{run}/exports/nodes_roles.csv")
    assert csv.status_code == 200 and gid in csv.text
    assert client.get(f"/api/runs/{run}/exports/result.json").status_code == 404
    assert client.get("/api/runs/not-a-run").status_code == 404
    resumed = TestClient(create_app(storage=storage))
    assert resumed.get(f"/api/runs/{run}").json()["n_nodes"] == 80
    files["edges"] = ("edges.parquet", b"corrupted", "application/octet-stream")
    assert resumed.post("/api/analyze", files=files).status_code == 422
    assert resumed.get(f"/api/runs/{run}").status_code == 200


def test_health_and_synthetic_default(tmp_path: Path):
    client = TestClient(create_app(storage=tmp_path))
    assert client.get("/health").json()["status"] == "ok"
    bootstrap = client.get("/api/bootstrap").json()
    assert bootstrap["synthetic"]
    assert not bootstrap["llm_enabled"]
    report = client.get(f"/api/runs/{bootstrap['run_id']}").json()
    assert report["synthetic"]
