"""Optional conversational graph analyst with bounded, read-only tool execution."""

import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from .analyst_tools import TOOL_SCHEMAS, execute_tool
from .assistant_context import (
    AssistantError,
    Conversations,
    valid_gid,
    validate_context,
    validate_request,
)
from .assistant_prompts import ANSWER_PROPERTIES, ANSWER_TOOL, INSTRUCTIONS, VIEWS
from .graph_query import LIMITATIONS

DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
MAX_TOOL_CALLS = 4
MAX_SECONDS = 75
CITATION = re.compile(r"\[gid:([^\]]+)\]")

# Public imports retained for callers validating identifiers before opening a conversation.
__all__ = ["AnalystAssistant", "AssistantError", "request_openai", "valid_gid", "validate_request"]


def request_openai(
    key: str,
    model: str,
    items: list[dict[str, Any]],
    *,
    final_only: bool = False,
    timeout: float = 30,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "store": False,
        "instructions": INSTRUCTIONS,
        "input": items,
        "tools": [ANSWER_TOOL] if final_only else [*TOOL_SCHEMAS, ANSWER_TOOL],
        "tool_choice": {"type": "function", "name": "answer_question"}
        if final_only
        else "required",
        "parallel_tool_calls": False,
        "max_output_tokens": 2500,
    }
    if model.startswith("gpt-5"):
        payload.update(
            {
                "reasoning": {"effort": "low"},
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": 4000,
            }
        )
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(1_048_577)
        if len(content) > 1_048_576:
            raise AssistantError(502, "Ответ AI слишком большой. Сократите вопрос")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except urllib.error.HTTPError as exc:
        # Never expose provider bodies: they may contain request content or credentials.
        if exc.code == 429:
            raise AssistantError(
                503, "Лимит или баланс OpenAI исчерпан. Повторите позже; анализ графа доступен"
            ) from None
        if exc.code in {401, 403}:
            raise AssistantError(
                503, "OpenAI отклонил ключ или доступ к модели. Проверьте настройки сервера"
            ) from None
        raise AssistantError(502, "OpenAI временно недоступен. Повторите позже") from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise AssistantError(
            504, "Не удалось дождаться OpenAI. Повторите позже; граф и выгрузки доступны"
        ) from None
    except (ValueError, UnicodeError):
        raise AssistantError(502, "Не удалось прочитать ответ OpenAI") from None


def _function_call(
    response: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    try:
        output = response["output"]
        if (
            response.get("status") != "completed"
            or not isinstance(output, list)
            or any(not isinstance(item, dict) for item in output)
        ):
            raise ValueError
        calls = [item for item in output if item.get("type") == "function_call"]
        if len(calls) != 1:
            raise ValueError
        call = calls[0]
        if (
            not isinstance(call.get("call_id"), str)
            or not 1 <= len(call["call_id"]) <= 200
            or not isinstance(call.get("name"), str)
        ):
            raise ValueError
        if not isinstance(call.get("arguments"), str) or len(call["arguments"]) > 30_000:
            raise ValueError
        arguments = json.loads(call["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError
        return output, call, arguments
    except (KeyError, TypeError, ValueError):
        raise AssistantError(
            502, "AI не сформировал корректный ответ. Уточните вопрос и повторите"
        ) from None


def _references(value: Any, known: set[str]) -> set[str]:
    if isinstance(value, str):
        return {value} if value in known else set()
    if isinstance(value, list):
        return set().union(*(_references(v, known) for v in value)) if value else set()
    if isinstance(value, dict):
        return _references(list(value.values()), known)
    return set()


def _validate_answer(answer: dict[str, Any], observed: set[str]) -> dict[str, Any]:
    def text(value: Any, maximum: int) -> bool:
        return isinstance(value, str) and 0 < len(value.strip()) <= maximum

    bad = "AI вернул неподтверждённые ссылки или некорректный ответ. Уточните вопрос"
    if set(answer) != set(ANSWER_PROPERTIES) or not text(answer["answer"], 8000):
        raise AssistantError(502, bad)
    claims, followups, actions = answer["claims"], answer["followups"], answer["actions"]
    if (
        not isinstance(claims, list)
        or len(claims) > 8
        or not isinstance(followups, list)
        or len(followups) > 3
        or any(not text(q, 300) for q in followups)
        or not isinstance(actions, list)
        or len(actions) > 5
    ):
        raise AssistantError(502, bad)
    for claim in claims:
        if (
            not isinstance(claim, dict)
            or set(claim) != {"text", "gids"}
            or not text(claim["text"], 1500)
            or not isinstance(claim["gids"], list)
            or len(claim["gids"]) > 20
            or any(not isinstance(gid, str) or gid not in observed for gid in claim["gids"])
        ):
            raise AssistantError(502, bad)
    for action in actions:
        if (
            not isinstance(action, dict)
            or set(action) != {"type", "label", "gid", "view"}
            or not text(action["label"], 120)
        ):
            raise AssistantError(502, bad)
        if action["type"] == "focus_node":
            if (
                not isinstance(action["gid"], str)
                or action["gid"] not in observed
                or action["view"] is not None
            ):
                raise AssistantError(502, bad)
        elif action["type"] == "show_view":
            if action["gid"] is not None or action["view"] not in VIEWS:
                raise AssistantError(502, bad)
        else:
            raise AssistantError(502, bad)
    prose = [answer["answer"], *[c["text"] for c in claims], *followups]
    if any(gid not in observed for line in prose for gid in CITATION.findall(line)):
        raise AssistantError(502, bad)
    return answer


class AnalystAssistant:
    def __init__(self) -> None:
        self.key = os.getenv("OPENAI_API_KEY", "").strip()
        self.reason = "Для AI настройте OPENAI_API_KEY или OPENAI_API_KEY_FILE на сервере"
        if not self.key and os.getenv("OPENAI_API_KEY_FILE"):
            try:
                content = Path(os.environ["OPENAI_API_KEY_FILE"]).read_text(encoding="utf-8")
                keys = re.findall(r"sk-[A-Za-z0-9_-]{20,}", content)
                if len(keys) == 1:
                    self.key = keys[0]
                else:
                    self.reason = "Файл ключа должен содержать один ключ OpenAI"
            except (OSError, UnicodeError):
                self.reason = "Не удалось прочитать OPENAI_API_KEY_FILE на сервере"
        self.model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.access_token = os.getenv("MONEY_GRAPH_ASSISTANT_TOKEN", "").strip()
        self.lock = threading.Lock()
        self.calls: deque[float] = deque()
        self.provider_calls: deque[float] = deque()
        self.conversations = Conversations()

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "reason": "" if self.enabled else self.reason,
            "access_required": bool(self.access_token),
        }

    @staticmethod
    def _budget(calls: deque[float], limit: int, message: str) -> None:
        now = time.monotonic()
        while calls and calls[0] <= now - 3600:
            calls.popleft()
        if len(calls) >= limit:
            raise AssistantError(429, message)
        calls.append(now)

    def ask(
        self, result: dict[str, Any], body: Any, token: str = "", run_id: str = "default"
    ) -> dict[str, Any]:
        if not self.enabled:
            raise AssistantError(503, self.reason)
        if self.access_token and not hmac.compare_digest(
            token.encode(), self.access_token.encode()
        ):
            raise AssistantError(401, "Введите код доступа к AI-ассистенту")
        rows = {row["gid"]: row for row in result["nodes"]}
        known = set(rows)
        question, selected = validate_request(body, known)
        context = validate_context(body.get("context"), result, known)
        if not self.lock.acquire(blocking=False):
            raise AssistantError(
                429, "AI уже обрабатывает вопрос. Повторите через несколько секунд"
            )
        try:
            cid, conversation = self.conversations.get(
                body.get("conversation_id"), run_id, result["report"]
            )
            self._budget(self.calls, 60, "Лимит ассистента: 60 вопросов в час. Повторите позже")
            envelope = {
                "question": question,
                "selected_gids": selected,
                "context": context,
                "recent_history": conversation.history,
                "graph_summary": {
                    field: result["report"].get(field)
                    for field in ("n_nodes", "n_edges", "n_seed", "n_transactions", "rules_version")
                },
            }
            items: list[dict[str, Any]] = [
                {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)}
            ]
            observed = (
                set(selected)
                | _references(context, known)
                | _references(
                    [
                        {"gids": turn["gids"], "facts": turn["verified_facts"]}
                        for turn in conversation.history
                    ],
                    known,
                )
            )
            output = self._run(result, question, selected, items, observed, known)
            output["conversation_id"] = cid
            cited = (
                _references(output["claims"], known)
                | _references(output["actions"], known)
                | set(CITATION.findall(output["answer"]))
            )
            # Preserve tool order for follow-ups such as "the first one", then add other verified refs.
            order = list(
                dict.fromkeys(
                    [
                        *output.pop("node_order"),
                        *sorted(cited, key=int),
                        *sorted(_references(output["facts"], known), key=int),
                    ]
                )
            )
            output["nodes"] = [
                {key: rows[gid][key] for key in ("gid", "role", "evidence")}
                for gid in order
                if gid in observed
            ]
            self.conversations.save(cid, conversation, question, output)
            return output
        finally:
            self.lock.release()

    def _run(
        self,
        result: dict[str, Any],
        question: str,
        selected: list[str],
        items: list[dict[str, Any]],
        observed: set[str],
        known: set[str],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + MAX_SECONDS
        usage = {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens")}
        steps: list[dict[str, Any]] = []
        facts: list[Any] = []
        node_order: list[str] = []
        limitations = list(LIMITATIONS)
        seen: set[str] = set()
        checked_selection = False
        for index in range(MAX_TOOL_CALLS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssistantError(
                    504, "Проверки заняли слишком много времени. Сузьте вопрос и повторите"
                )
            self._budget(
                self.provider_calls, 180, "Лимит вычислений AI за час исчерпан. Повторите позже"
            )
            response = request_openai(
                self.key,
                self.model,
                items,
                final_only=index == MAX_TOOL_CALLS,
                timeout=min(30, remaining),
            )
            for field in usage:
                value = response.get("usage", {})
                value = value.get(field) if isinstance(value, dict) else None
                if type(value) is int and value >= 0:
                    usage[field] += value
            if time.monotonic() > deadline:
                raise AssistantError(
                    504, "Проверки заняли слишком много времени. Сузьте вопрос и повторите"
                )
            returned, call, arguments = _function_call(response)
            name = call["name"]
            if name == "answer_question":
                answer = _validate_answer(arguments, observed)
                return {
                    **answer,
                    "facts": facts,
                    "node_order": node_order,
                    "limitations": list(dict.fromkeys(limitations)),
                    "query": {"steps": steps},
                    "model": self.model,
                    "usage": usage,
                }
            if index == MAX_TOOL_CALLS:
                raise AssistantError(502, "AI превысил число проверок. Сузьте вопрос и повторите")
            signature = json.dumps([name, arguments], sort_keys=True)
            try:
                if signature in seen:
                    raise ValueError(
                        "Эта проверка уже выполнена. Используй её результат либо уточни аргументы"
                    )
                seen.add(signature)
                if (
                    not checked_selection
                    and selected
                    and re.search(
                        r"\b(?:выбран\w*|выделен\w*|этих|эти|этим|этими)\b", question, re.I
                    )
                    and not (set(re.findall(r"(?<![\w-])-?\d+(?!\w)", question)) & known)
                    and name in {"find_nodes", "inspect_nodes", "trace_flows"}
                ):
                    gids = arguments.get("gids")
                    if (
                        not isinstance(gids, list)
                        or any(not isinstance(g, str) for g in gids)
                        or set(gids) != set(selected)
                    ):
                        raise ValueError(
                            "Сохрани ВСЮ выбранную группу selected_gids в первой проверке; не подменяй её одним узлом или всем графом"
                        )
                    checked_selection = True
                result_part = execute_tool(result, name, arguments)
            except ValueError as exc:
                result_part = {
                    "error": str(exc),
                    "instruction": "Исправь запрос либо честно объясни ограничение. Эта проверка не дала фактов.",
                }
            else:
                observed.update(_references(result_part, known))
                node_order.extend(row["gid"] for row in result_part.get("nodes", []))
                facts.extend(result_part.get("facts", []))
                limitations.extend(result_part.get("limitations", []))
            steps.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "summary": result_part.get("answer", result_part.get("error", "")),
                    "ok": "error" not in result_part,
                }
            )
            items.extend(returned)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(result_part, ensure_ascii=False),
                }
            )
        raise AssertionError("Unreachable: the final turn must answer or fail")
