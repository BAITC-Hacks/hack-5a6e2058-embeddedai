import copy
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from money_graph import api, assistant
from money_graph.assistant import AssistantError
from money_graph.loader import DataError


@pytest.fixture(autouse=True)
def no_real_credentials_or_network(monkeypatch):
    for name in list(os.environ):
        if name.startswith("OPENAI_") or name == "MONEY_GRAPH_ASSISTANT_TOKEN":
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        raise AssertionError("API tests must never contact a real provider")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def client_for(tmp_path):
    client = TestClient(api.create_app(storage=tmp_path))
    run = client.get("/api/bootstrap").json()["run_id"]
    result = json.loads((tmp_path / run / "result.json").read_text(encoding="utf-8"))
    return client, run, result


def provider_response(gid=None):
    return {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "name": "answer_question",
                "call_id": "answer_1",
                "arguments": json.dumps(
                    {
                        "answer": f"Проверьте [gid:{gid}]."
                        if gid
                        else "Уточните узлы для сравнения.",
                        "claims": [{"text": "Проверяемый узел", "gids": [gid]}] if gid else [],
                        "followups": ["Какие связи проверить дальше?"],
                        "actions": [
                            {"type": "focus_node", "label": "Открыть", "gid": gid, "view": None}
                        ]
                        if gid
                        else [],
                    }
                ),
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    }


def test_queue_endpoint_has_complete_role_filtering_pagination_and_exact_gid_search(tmp_path):
    client, run, result = client_for(tmp_path)
    expected = sorted(
        (row for row in result["nodes"] if row["role"] == "peripheral"), key=lambda row: row["rank"]
    )
    response = client.get(
        f"/api/runs/{run}/nodes", params={"role": "peripheral", "offset": 1, "limit": 2}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 80 and payload["matched"] == len(expected)
    assert [row["gid"] for row in payload["nodes"]] == [row["gid"] for row in expected[1:3]]
    assert [row["rank"] for row in payload["nodes"]] == [row["rank"] for row in expected[1:3]]
    selected = expected[0]
    searched = client.get(
        f"/api/runs/{run}/nodes",
        params={
            "search": f" {selected['gid']} ",
            "cluster": selected["cluster_id"],
            "depth": selected["depth"],
        },
    ).json()
    assert searched["matched"] == 1 and searched["nodes"][0]["gid"] == selected["gid"]
    assert isinstance(searched["nodes"][0]["gid"], str)
    seeds = client.get(f"/api/runs/{run}/nodes", params={"seeds": "true", "limit": 200}).json()
    assert len(seeds["nodes"]) == result["report"]["n_seed"]
    assert all(row["is_seed"] for row in seeds["nodes"])
    assert response.headers["cache-control"] == "no-store"


def test_queue_parameter_errors_and_unknown_run_are_actionable(tmp_path):
    client, run, _ = client_for(tmp_path)
    for params in (
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"depth": 5},
        {"depth": -1},
        {"search": "x" * 31},
        {"sort": "sql"},
        {"role": "fake"},
        {"order": "bad"},
    ):
        response = client.get(f"/api/runs/{run}/nodes", params=params)
        assert response.status_code == 422 and "detail" in response.json()
    assert client.get("/api/runs/missing/nodes").status_code == 404


def test_robustness_cache_reuses_valid_snapshot_and_invalidates_on_replacement(
    tmp_path, monkeypatch
):
    calls = []

    def diagnostic(result):
        calls.append(result)
        return {"caveat": "test diagnostic", "generation": len(calls)}

    monkeypatch.setattr(api, "analyze_robustness", diagnostic)
    client, run, result = client_for(tmp_path)
    endpoint = f"/api/runs/{run}/robustness"
    assert client.get(endpoint).json()["generation"] == 1
    assert client.get(endpoint).json()["generation"] == 1
    assert len(calls) == 1
    path = tmp_path / run / "result.json"
    result["report"]["runtime_seconds"] += 1
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    assert client.get(endpoint).json()["generation"] == 2
    assert calls[0] is not calls[1]
    path.write_text("{broken", encoding="utf-8")
    assert client.get(endpoint).status_code == 503
    assert len(calls) == 2


def test_robustness_failure_returns_diagnostic_and_releases_gate(tmp_path, monkeypatch):
    client, run, _ = client_for(tmp_path)

    def failed(result):
        raise DataError("Не удалось проверить сообщества")

    monkeypatch.setattr(api, "analyze_robustness", failed)
    response = client.get(f"/api/runs/{run}/robustness")
    assert response.status_code == 503
    assert "Не удалось проверить" in response.json()["detail"]
    monkeypatch.setattr(api, "analyze_robustness", lambda result: {"caveat": "recovered"})
    assert client.get(f"/api/runs/{run}/robustness").json()["caveat"] == "recovered"


def test_robustness_gate_prevents_concurrent_recalculation(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def diagnostic(result):
        started.set()
        assert release.wait(10)
        return {"caveat": "complete"}

    monkeypatch.setattr(api, "analyze_robustness", diagnostic)
    client, run, _ = client_for(tmp_path)
    endpoint = f"/api/runs/{run}/robustness"
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.get, endpoint)
        try:
            assert started.wait(5)
            second = client.get(endpoint)
            assert second.status_code == 429
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200


def test_html_export_is_attachment_and_legacy_runs_are_rendered_without_reanalysis(
    tmp_path, monkeypatch
):
    client, run, result = client_for(tmp_path)
    endpoint = f"/api/runs/{run}/exports/report.html"
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="report.html"'
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert result["nodes"][0]["gid"] in response.text
    path = tmp_path / run / "report.html"
    path.unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy HTML should reuse the existing result")

    monkeypatch.setattr(api, "analyze", forbidden)
    fallback = client.get(endpoint)
    assert fallback.status_code == 200 and fallback.content == response.content
    assert not path.exists()
    assert client.get(f"/api/runs/{run}/exports/run_report.json").status_code == 404
    assert client.get("/api/runs/" + "0" * 32 + "/exports/report.html").status_code == 404


def test_existing_html_remains_downloadable_when_result_is_corrupt(tmp_path):
    client, run, _ = client_for(tmp_path)
    (tmp_path / run / "result.json").write_text("{broken", encoding="utf-8")
    assert client.get(f"/api/runs/{run}").status_code == 503
    assert client.get(f"/api/runs/{run}/exports/report.html").status_code == 200
    (tmp_path / run / "report.html").unlink()
    assert client.get(f"/api/runs/{run}/exports/report.html").status_code == 503


def test_assistant_disabled_is_optional_and_other_graph_features_still_work(tmp_path):
    client, run, _ = client_for(tmp_path)
    assert client.get("/api/assistant/status").json()["enabled"] is False
    assert client.get("/api/bootstrap").json()["llm_enabled"] is False
    response = client.post(
        f"/api/runs/{run}/assistant", json={"question": "Кто первый?", "selected_gids": []}
    )
    assert response.status_code == 503 and "OPENAI_API_KEY" in response.json()["detail"]
    assert client.get(f"/api/runs/{run}/nodes").status_code == 200
    assert client.get(f"/api/runs/{run}/exports/nodes_roles.csv").status_code == 200


def test_assistant_endpoint_checks_access_token_and_returns_only_real_node_references(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    monkeypatch.setenv("MONEY_GRAPH_ASSISTANT_TOKEN", "unit-access-code")
    calls = []

    def provider(*args, **kwargs):
        calls.append(args)
        return provider_response()

    monkeypatch.setattr(assistant, "request_openai", provider)
    client, run, result = client_for(tmp_path)
    status = client.get("/api/assistant/status").json()
    assert status["enabled"] and status["access_required"]
    assert "unit-test-provider-key" not in json.dumps(status)
    endpoint = f"/api/runs/{run}/assistant"
    body = {"question": "Кто первый?", "selected_gids": []}
    for headers in ({}, {"X-Assistant-Token": "wrong"}):
        assert client.post(endpoint, json=body, headers=headers).status_code == 401
    assert calls == []
    response = client.post(endpoint, json=body, headers={"X-Assistant-Token": "unit-access-code"})
    assert response.status_code == 200
    known = {row["gid"] for row in result["nodes"]}
    assert all(row["gid"] in known for row in response.json()["nodes"])
    assert all(isinstance(row["gid"], str) for row in response.json()["nodes"])
    assert "unit-access-code" not in response.text and "unit-test-provider-key" not in response.text
    assert len(calls) == 1


def test_assistant_endpoint_rejects_invalid_question_without_calling_provider(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    calls = []
    monkeypatch.setattr(assistant, "request_openai", lambda *args, **kwargs: calls.append(args))
    client, run, _ = client_for(tmp_path)
    endpoint = f"/api/runs/{run}/assistant"
    for body in (
        {"question": "", "selected_gids": []},
        {"question": "Кто?", "selected_gids": [9007199254741001]},
        {"question": "Кто?", "selected_gids": ["999999"]},
        {"question": "Кто?", "selected_gids": [], "extra": True},
    ):
        assert client.post(endpoint, json=body).status_code == 422
    assert (
        client.post(
            endpoint, content="{broken", headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/runs/missing/assistant", json={"question": "Кто?", "selected_gids": []}
        ).status_code
        == 404
    )
    assert calls == []


def test_assistant_provider_timeout_and_invalid_output_become_actionable_http_errors(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    client, run, _ = client_for(tmp_path)
    endpoint = f"/api/runs/{run}/assistant"
    body = {"question": "Кто первый?", "selected_gids": []}

    def timeout(*args, **kwargs):
        raise AssistantError(504, "Не удалось дождаться OpenAI")

    monkeypatch.setattr(assistant, "request_openai", timeout)
    assert client.post(endpoint, json=body).status_code == 504
    monkeypatch.setattr(
        assistant,
        "request_openai",
        lambda *args, **kwargs: {"status": "completed", "output": [None]},
    )
    assert client.post(endpoint, json=body).status_code == 502
    monkeypatch.setattr(assistant, "request_openai", lambda *args, **kwargs: provider_response())
    assert client.post(endpoint, json=body).status_code == 200


def test_assistant_endpoint_cannot_hallucinate_a_known_unselected_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    client, run, result = client_for(tmp_path)
    selected, hallucinated = [row["gid"] for row in result["nodes"][:2]]
    monkeypatch.setattr(
        assistant, "request_openai", lambda *args, **kwargs: provider_response(hallucinated)
    )
    response = client.post(
        f"/api/runs/{run}/assistant",
        json={"question": "Объясни выбранный узел", "selected_gids": [selected]},
    )
    assert response.status_code == 502 and "неподтверждённые" in response.json()["detail"]


def test_assistant_concurrent_endpoint_requests_make_only_one_paid_call(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    started, release = threading.Event(), threading.Event()
    calls = []

    def provider(*args, **kwargs):
        calls.append(args)
        started.set()
        assert release.wait(10)
        return copy.deepcopy(provider_response())

    monkeypatch.setattr(assistant, "request_openai", provider)
    client, run, _ = client_for(tmp_path)
    endpoint = f"/api/runs/{run}/assistant"
    body = {"question": "Кто первый?", "selected_gids": []}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, endpoint, json=body)
        try:
            assert started.wait(5)
            assert client.post(endpoint, json=body).status_code == 429
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
    assert len(calls) == 1


def test_assistant_api_preserves_followup_context_and_rejects_changed_snapshot(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    client, run, result = client_for(tmp_path)
    selected, opened = [row["gid"] for row in result["nodes"][:2]]
    calls = []

    def provider(key, model, items, **kwargs):
        calls.append(copy.deepcopy(items))
        return provider_response(opened)

    monkeypatch.setattr(assistant, "request_openai", provider)
    endpoint = f"/api/runs/{run}/assistant"
    context = {
        "active_gid": opened,
        "active_tab": "queue",
        "filters": {"role": None, "cluster": None, "depth": None, "seeds": False},
    }
    first = client.post(
        endpoint,
        json={
            "question": "Объясни открытую карточку",
            "selected_gids": [selected],
            "context": context,
        },
    )
    assert first.status_code == 200 and first.json()["nodes"][0]["gid"] == opened
    assert first.headers["cache-control"] == "no-store"
    cid = first.json()["conversation_id"]
    second = client.post(
        endpoint,
        json={"question": "А что с ним дальше?", "selected_gids": [], "conversation_id": cid},
    )
    assert second.status_code == 200 and second.json()["conversation_id"] == cid
    assert opened in json.dumps(json.loads(calls[1][0]["content"])["recent_history"])
    result["report"]["runtime_seconds"] += 1
    (tmp_path / run / "result.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    stale = client.post(
        endpoint, json={"question": "Продолжай", "selected_gids": [], "conversation_id": cid}
    )
    assert stale.status_code == 409 and len(calls) == 2


def test_assistant_api_conversation_cannot_cross_run_boundary(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    client, run, _ = client_for(tmp_path)
    calls = []

    def provider(*args, **kwargs):
        calls.append(1)
        return provider_response()

    monkeypatch.setattr(assistant, "request_openai", provider)
    initial = client.post(
        f"/api/runs/{run}/assistant", json={"question": "Объясни граф", "selected_gids": []}
    ).json()
    other = "f" * 32
    shutil.copytree(tmp_path / run, tmp_path / other)
    response = client.post(
        f"/api/runs/{other}/assistant",
        json={
            "question": "Продолжи",
            "selected_gids": [],
            "conversation_id": initial["conversation_id"],
        },
    )
    assert response.status_code == 409 and len(calls) == 1
