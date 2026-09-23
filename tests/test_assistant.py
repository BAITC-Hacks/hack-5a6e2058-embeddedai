import copy
import io
import json
import os
import urllib.error
from collections import deque
from email.message import Message
from typing import Any

import pytest

from money_graph import assistant
from money_graph.assistant import (
    AnalystAssistant,
    AssistantError,
    request_openai,
    valid_gid,
    validate_plan,
    validate_request,
)
from money_graph.demo import create_demo
from money_graph.pipeline import analyze


@pytest.fixture(autouse=True)
def no_real_credentials_or_network(monkeypatch):
    for name in list(os.environ):
        if name.startswith("OPENAI_") or name == "MONEY_GRAPH_ASSISTANT_TOKEN":
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must never contact OpenAI")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    source = tmp_path_factory.mktemp("assistant-data")
    create_demo(source)
    return analyze(source)


def plan(**changes):
    return {
        "operation": "rank_nodes",
        "gids": [],
        "role": "all",
        "limit": 3,
        "max_hops": 1,
        "min_sources": 0,
        "sort_by": "priority_score",
        "clarification": "",
    } | changes


def provider_response(query=None, **changes):
    return {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "name": "query_graph",
                "arguments": json.dumps(query or plan()),
            }
        ],
        "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    } | changes


def configured(monkeypatch, response=None):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-provider-key")
    captured = []

    def provider(key, model, question, selected):
        captured.append({"key": key, "model": model, "question": question, "selected": selected})
        return copy.deepcopy(response if response is not None else provider_response())

    monkeypatch.setattr(assistant, "request_openai", provider)
    return AnalystAssistant(), captured


def test_valid_tool_call_returns_computed_graph_facts_and_usage_not_provider_prose(
    result, monkeypatch
):
    payload = provider_response()
    payload["output"].append({"type": "message", "text": "Fabricated guilt and amounts"})
    service, calls = configured(monkeypatch, payload)
    output = service.ask(result, {"question": "  Кто первый по приоритету?  ", "selected_gids": []})
    assert calls[0]["question"] == "Кто первый по приоритету?"
    expected = sorted(result["nodes"], key=lambda node: node["rank"])[:3]
    assert [row["gid"] for row in output["facts"]] == [row["gid"] for row in expected]
    assert all(
        row["value"] == node["priority_score"]
        for row, node in zip(output["facts"], expected, strict=True)
    )
    assert output["usage"] == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    assert output["model"] == service.model
    assert "Fabricated" not in json.dumps(output)
    assert "unit-test-provider-key" not in json.dumps(output)
    assert not service.lock.locked()


def test_no_key_status_and_disabled_ask_make_no_provider_call(result):
    service = AnalystAssistant()
    assert not service.enabled
    assert service.status()["enabled"] is False
    with pytest.raises(AssistantError) as error:
        service.ask(result, {"question": "Кто первый?", "selected_gids": []})
    assert error.value.status == 503
    assert "OPENAI_API_KEY" in str(error.value)


def test_key_file_reads_exactly_one_key_without_exposing_it(tmp_path, monkeypatch):
    key = "sk-" + "x" * 30
    path = tmp_path / "key.txt"
    path.write_text(f"Owner note\n{key}\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(path))
    service = AnalystAssistant()
    assert service.key == key and service.enabled
    assert key not in json.dumps(service.status())
    path.write_text(f"{key}\n{key}\n", encoding="utf-8")
    assert not AnalystAssistant().enabled
    path.unlink()
    assert not AnalystAssistant().enabled


def test_access_token_checked_before_provider_call_and_never_returned(result, monkeypatch):
    monkeypatch.setenv("MONEY_GRAPH_ASSISTANT_TOKEN", "код-доступа")
    service, calls = configured(monkeypatch)
    assert service.status()["access_required"] is True
    for token in ("", "wrong"):
        with pytest.raises(AssistantError) as error:
            service.ask(result, {"question": "Кто первый?", "selected_gids": []}, token)
        assert error.value.status == 401
    assert calls == [] and len(service.calls) == 0
    answer = service.ask(result, {"question": "Кто первый?", "selected_gids": []}, "код-доступа")
    assert "код-доступа" not in json.dumps(answer, ensure_ascii=False)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"question": "hi"},
        {"question": "hi", "selected_gids": [], "extra": True},
        {"question": " ", "selected_gids": []},
        {"question": "x" * 2001, "selected_gids": []},
        {"question": 12, "selected_gids": []},
        {"question": "hi", "selected_gids": "1"},
        {"question": "hi", "selected_gids": [1]},
        {"question": "hi", "selected_gids": ["01"]},
        {"question": "hi", "selected_gids": ["1"] * 21},
        {"question": "hi", "selected_gids": ["9223372036854775808"]},
        {"question": "hi", "selected_gids": ["999"]},
    ],
)
def test_request_validation_rejects_invalid_shapes_and_unknown_ids_before_paid_call(
    result, monkeypatch, body
):
    service, calls = configured(monkeypatch)
    with pytest.raises(AssistantError) as error:
        service.ask(result, body)
    assert error.value.status == 422
    assert calls == [] and len(service.calls) == 0


def test_gid_validation_uses_signed_int64_strings_without_float_rounding():
    for value in ("0", "-9223372036854775808", "9223372036854775807", "9007199254741001"):
        assert valid_gid(value)
    for invalid in (
        False,
        1,
        1.0,
        "-0",
        "+1",
        "01",
        " 1",
        "1.0",
        "1e3",
        "9223372036854775808",
        "-9223372036854775809",
    ):
        assert not valid_gid(invalid)
    assert validate_request({"question": " hi ", "selected_gids": ["1", "1"]}, {"1"}) == (
        "hi",
        ["1"],
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "shell"},
        {"operation": []},
        {"role": "guilty"},
        {"sort_by": "sql"},
        {"limit": 0},
        {"limit": True},
        {"limit": 21},
        {"max_hops": 5},
        {"min_sources": -1},
        {"gids": [1]},
        {"gids": ["01"]},
        {"gids": ["1"] * 21},
        {"clarification": None},
        {"clarification": "x" * 601},
        {"unexpected": "extra"},
    ],
)
def test_plan_schema_rejects_provider_values(changes):
    with pytest.raises(AssistantError) as error:
        validate_plan(plan(**changes), "Узел 1", ["1"], {"1"})
    assert error.value.status == 502


def test_plan_rejects_hallucinated_known_gid_and_distinguishes_unknown_mentioned_gid():
    with pytest.raises(AssistantError) as hallucinated:
        validate_plan(plan(operation="node_summary", gids=["2"]), "Этот узел", ["1"], {"1", "2"})
    assert hallucinated.value.status == 502
    with pytest.raises(AssistantError) as unknown:
        validate_plan(plan(operation="node_summary", gids=["999"]), "Узел 999", [], {"1"})
    assert unknown.value.status == 422
    accepted = validate_plan(
        plan(operation="node_summary", gids=["-12", "-12"]), "Узел -12", [], {"-12"}
    )
    assert accepted["gids"] == ["-12"]


@pytest.mark.parametrize(
    "query",
    [
        plan(operation="node_summary"),
        plan(operation="path", gids=["1"]),
        plan(operation="path", gids=["1", "1"]),
        plan(operation="common_recipients", gids=["1"], min_sources=2),
    ],
)
def test_plan_rejects_missing_or_inconsistent_selection(query):
    with pytest.raises(AssistantError) as error:
        validate_plan(query, "Узел 1", ["1"], {"1"})
    assert error.value.status == 422


@pytest.mark.parametrize(
    "question,query",
    [
        (
            "Кто собирает деньги с этих пятерых?",
            plan(operation="common_recipients", gids=["1"]),
        ),
        ("Кто из выбранных первым?", plan(operation="rank_nodes", gids=[])),
    ],
)
def test_plan_cannot_silently_change_explicitly_selected_group(question, query):
    selected = ["1", "2", "3", "4", "5"]
    with pytest.raises(AssistantError) as error:
        validate_plan(query, question, selected, set(selected))
    assert error.value.status == 422


def test_plan_cannot_ignore_unsupported_filter_on_community():
    with pytest.raises(AssistantError) as error:
        validate_plan(
            plan(operation="community", gids=["1"], role="consolidator"),
            "Покажи только консолидаторов сообщества узла 1",
            ["1"],
            {"1"},
        )
    assert error.value.status == 422


@pytest.mark.parametrize(
    "response",
    [
        provider_response(status="incomplete"),
        provider_response(status="failed"),
        provider_response(output=[]),
        provider_response(output=[None]),
        provider_response(output="not-a-list"),
        provider_response(output=[{"type": "function_call", "name": "evil", "arguments": "{}"}]),
        provider_response(
            output=[{"type": "function_call", "name": "query_graph", "arguments": "broken"}]
        ),
        provider_response(
            output=[{"type": "function_call", "name": "query_graph", "arguments": "null"}]
        ),
        provider_response(output=provider_response()["output"] * 2),
        {},
    ],
)
def test_malformed_or_incomplete_provider_response_is_actionable_and_releases_gate(
    result, monkeypatch, response
):
    service, _ = configured(monkeypatch, response)
    with pytest.raises(AssistantError) as error:
        service.ask(result, {"question": "Кто первый?", "selected_gids": []})
    assert error.value.status == 502
    assert not service.lock.locked()


@pytest.mark.parametrize(
    "usage",
    [None, [], "invalid", {"input_tokens": -1, "output_tokens": True, "total_tokens": "20"}],
)
def test_invalid_provider_usage_never_turns_successful_graph_answer_into_server_error(
    result, monkeypatch, usage
):
    service, _ = configured(monkeypatch, provider_response(usage=usage))
    answer = service.ask(result, {"question": "Кто первый?", "selected_gids": []})
    assert answer["facts"]
    assert answer["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def test_rate_limit_and_concurrency_prevent_paid_requests_and_expire(result, monkeypatch):
    service, calls = configured(monkeypatch)
    body = {"question": "Кто первый?", "selected_gids": []}
    service.lock.acquire()
    try:
        with pytest.raises(AssistantError) as busy:
            service.ask(result, body)
        assert busy.value.status == 429
    finally:
        service.lock.release()
    monkeypatch.setattr(assistant.time, "monotonic", lambda: 10_000.0)
    service.calls = deque([9_999.0] * 60)
    with pytest.raises(AssistantError) as limited:
        service.ask(result, body)
    assert limited.value.status == 429 and calls == []
    service.calls = deque([6_400.0] * 60)
    assert service.ask(result, body)["facts"]
    assert len(calls) == 1 and len(service.calls) == 1


def test_provider_failure_still_counts_budget_and_releases_lock(result, monkeypatch):
    service, _ = configured(monkeypatch)

    def failed(*args):
        raise AssistantError(504, "Provider timeout")

    monkeypatch.setattr(assistant, "request_openai", failed)
    with pytest.raises(AssistantError) as error:
        service.ask(result, {"question": "Кто первый?", "selected_gids": []})
    assert error.value.status == 504 and len(service.calls) == 1
    assert not service.lock.locked()


def test_openai_request_sends_only_question_selection_and_explicit_bounded_tool_contract(
    monkeypatch,
):
    captured: dict[str, Any] = {}

    def send(request, timeout):
        captured.update(
            url=request.full_url,
            headers=request.headers,
            payload=json.loads(request.data),
            timeout=timeout,
        )
        return io.BytesIO(json.dumps(provider_response()).encode())

    monkeypatch.setattr("urllib.request.urlopen", send)
    assert (
        request_openai("unit-key", "unit-model", "Кто получает?", ["9007199254741001"])["status"]
        == "completed"
    )
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["timeout"] == 30
    payload = captured["payload"]
    assert payload["store"] is False and payload["parallel_tool_calls"] is False
    assert payload["tool_choice"] == {"type": "function", "name": "query_graph"}
    assert len(payload["tools"]) == 1 and payload["tools"][0]["strict"] is True
    assert payload["tools"][0]["parameters"]["additionalProperties"] is False
    assert json.loads(payload["input"]) == {
        "question": "Кто получает?",
        "selected_gids": ["9007199254741001"],
    }
    assert payload["max_output_tokens"] == 1400


@pytest.mark.parametrize(
    "failure,status",
    [(TimeoutError(), 504), (urllib.error.URLError("offline"), 504), (OSError("offline"), 504)],
)
def test_transport_errors_return_actionable_timeout(monkeypatch, failure, status):
    def send(*args, **kwargs):
        raise failure

    monkeypatch.setattr("urllib.request.urlopen", send)
    with pytest.raises(AssistantError) as error:
        request_openai("unit-key", "unit-model", "Вопрос", [])
    assert error.value.status == status


@pytest.mark.parametrize("code,status", [(429, 503), (401, 503), (403, 503), (500, 502)])
def test_provider_http_error_does_not_leak_body_or_credentials(monkeypatch, code, status):
    def send(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://unit.invalid",
            code,
            "secret-provider-message",
            Message(),
            io.BytesIO(b"secret-key-and-private-question"),
        )

    monkeypatch.setattr("urllib.request.urlopen", send)
    with pytest.raises(AssistantError) as error:
        request_openai("secret-key", "unit-model", "private-question", [])
    assert error.value.status == status
    assert "secret" not in str(error.value) and "private-question" not in str(error.value)


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"\xff", b"x" * 1_048_577])
def test_invalid_or_oversized_provider_body_is_rejected(monkeypatch, body):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(body))
    with pytest.raises(AssistantError) as error:
        request_openai("unit-key", "unit-model", "Вопрос", [])
    assert error.value.status == 502
