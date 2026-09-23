"""Optional OpenAI interpretation, strict validation and a bounded paid-call budget."""

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

from .assistant_prompts import INSTRUCTIONS, OPERATIONS, PROPERTIES, QUERY_TOOL, SORT_FIELDS
from .roles import LABELS

DEFAULT_MODEL = "gpt-4.1-mini-2025-04-14"
GID = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")


class AssistantError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def valid_gid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 20
        and GID.fullmatch(value) is not None
        and -(2**63) <= int(value) < 2**63
    )


def validate_request(body: Any, known_gids: set[str]) -> tuple[str, list[str]]:
    if not isinstance(body, dict) or set(body) != {"question", "selected_gids"}:
        raise AssistantError(422, "Нужны question и selected_gids")
    question, selected = body["question"], body["selected_gids"]
    if not isinstance(question, str) or not 2 <= len(question.strip()) <= 2000:
        raise AssistantError(422, "Вопрос должен содержать от 2 до 2000 символов")
    if (
        not isinstance(selected, list)
        or len(selected) > 20
        or any(not valid_gid(gid) for gid in selected)
    ):
        raise AssistantError(422, "Выберите до 20 узлов; gid должны быть строками int64")
    if any(gid not in known_gids for gid in selected):
        raise AssistantError(422, "Один из выбранных gid отсутствует в текущем графе")
    return question.strip(), list(dict.fromkeys(selected))


def validate_plan(plan: Any, question: str, selected: list[str], known: set[str]) -> dict[str, Any]:
    message = "Модель вернула некорректный запрос. Уточните вопрос и повторите"
    if not isinstance(plan, dict) or set(plan) != set(PROPERTIES):
        raise AssistantError(502, message)
    if (
        plan["operation"] not in OPERATIONS
        or plan["role"] not in ["all", *LABELS]
        or plan["sort_by"] not in SORT_FIELDS
    ):
        raise AssistantError(502, message)
    for field, low, high in (("limit", 1, 20), ("max_hops", 1, 4), ("min_sources", 0, 20)):
        if type(plan[field]) is not int or not low <= plan[field] <= high:
            raise AssistantError(502, message)
    if not isinstance(plan["clarification"], str) or len(plan["clarification"]) > 600:
        raise AssistantError(502, message)
    gids = plan["gids"]
    if not isinstance(gids, list) or len(gids) > 20 or any(not valid_gid(gid) for gid in gids):
        raise AssistantError(502, message)
    # A model may select identifiers, but cannot invent references to other known clients.
    mentioned = set(re.findall(r"(?<![\w-])-?\d+(?!\w)", question))
    if any(gid not in set(selected) | mentioned for gid in gids):
        raise AssistantError(
            502, "Модель указала gid вне вопроса и выбранных узлов. Уточните запрос"
        )
    if any(gid not in known for gid in gids):
        raise AssistantError(422, "Указанный в вопросе gid отсутствует в текущем графе")
    plan["gids"] = list(dict.fromkeys(gids))
    operation = plan["operation"]
    refers_to_selection = (
        re.search(
            r"\b(?:выбран\w*|выделен\w*|эти\w*|этих|этим|этими|них|ним|ними)\b",
            question,
            flags=re.IGNORECASE,
        )
        is not None
    )
    if (
        selected
        and not (mentioned & known)
        and operation != "unsupported"
        and (operation != "rank_nodes" or refers_to_selection)
        and set(plan["gids"]) != set(selected)
    ):
        raise AssistantError(
            422,
            "AI изменил состав выбранных узлов. Укажите нужные gid явно или повторите вопрос для всех выбранных",
        )
    if operation != "unsupported" and (
        (
            plan["role"] != "all"
            and operation not in {"rank_nodes", "common_recipients", "common_senders"}
        )
        or (plan["sort_by"] != "priority_score" and operation != "rank_nodes")
        or (plan["min_sources"] != 0 and operation not in {"common_recipients", "common_senders"})
        or (plan["max_hops"] != 1 and operation in {"rank_nodes", "node_summary", "community"})
    ):
        raise AssistantError(
            422,
            "Такое сочетание фильтра и операции не поддерживается. Уточните вопрос; рейтинг поддерживает роль и сортировку по сумме",
        )
    if plan["operation"] not in {"rank_nodes", "unsupported"} and not gids:
        raise AssistantError(422, "Выберите узлы в очереди или укажите их gid в вопросе")
    if plan["operation"] == "path" and len(plan["gids"]) != 2:
        raise AssistantError(422, "Для пути нужны два разных gid: отправитель и получатель")
    if plan["operation"] in {"common_recipients", "common_senders"} and plan["min_sources"] > len(
        plan["gids"]
    ):
        raise AssistantError(422, "Число источников больше количества выбранных узлов")
    return plan


def request_openai(key: str, model: str, question: str, selected: list[str]) -> dict[str, Any]:
    payload = {
        "model": model,
        "store": False,
        "instructions": INSTRUCTIONS,
        "input": json.dumps({"question": question, "selected_gids": selected}, ensure_ascii=False),
        "tools": [QUERY_TOOL],
        "tool_choice": {"type": "function", "name": "query_graph"},
        "parallel_tool_calls": False,
        "max_output_tokens": 1400,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(1_048_577)
        if len(content) > 1_048_576:
            raise AssistantError(502, "Ответ AI слишком большой. Сократите вопрос")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except urllib.error.HTTPError as exc:
        # Provider bodies can contain sensitive request content; never expose or log them.
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


class AnalystAssistant:
    def __init__(self) -> None:
        self.key = os.getenv("OPENAI_API_KEY", "").strip()
        self.reason = "Для AI настройте OPENAI_API_KEY или OPENAI_API_KEY_FILE на сервере"
        if not self.key and os.getenv("OPENAI_API_KEY_FILE"):
            try:
                text = Path(os.environ["OPENAI_API_KEY_FILE"]).read_text(encoding="utf-8")
                keys = re.findall(r"sk-[A-Za-z0-9_-]{20,}", text)
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

    def ask(self, result: dict[str, Any], body: Any, token: str = "") -> dict[str, Any]:
        # Import lazily: the CLI and optional-disabled path do not use AI integrations.
        from .graph_query import execute_query

        if not self.enabled:
            raise AssistantError(503, self.reason)
        if self.access_token and not hmac.compare_digest(
            token.encode(), self.access_token.encode()
        ):
            raise AssistantError(401, "Введите код доступа к AI-ассистенту")
        known = {row["gid"] for row in result["nodes"]}
        question, selected = validate_request(body, known)
        if not self.lock.acquire(blocking=False):
            raise AssistantError(
                429, "AI уже обрабатывает вопрос. Повторите через несколько секунд"
            )
        try:
            now = time.monotonic()
            while self.calls and self.calls[0] <= now - 3600:
                self.calls.popleft()
            if len(self.calls) >= 60:
                raise AssistantError(429, "Лимит ассистента: 60 запросов в час. Повторите позже")
            self.calls.append(now)
            response = request_openai(self.key, self.model, question, selected)
            try:
                output = response["output"]
                if not isinstance(output, list) or any(
                    not isinstance(item, dict) for item in output
                ):
                    raise ValueError
                calls = [item for item in output if item.get("type") == "function_call"]
                if (
                    response.get("status") != "completed"
                    or len(calls) != 1
                    or calls[0].get("name") != "query_graph"
                ):
                    raise ValueError
                plan = validate_plan(json.loads(calls[0]["arguments"]), question, selected, known)
            except (KeyError, TypeError, ValueError):
                raise AssistantError(
                    502, "AI не сформировал запрос к графу. Уточните вопрос"
                ) from None
            answer = execute_query(result, plan)
            usage = response.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            return {
                **answer,
                "query": plan,
                "model": self.model,
                "usage": {
                    field: value if type(value := usage.get(field)) is int and value >= 0 else 0
                    for field in ("input_tokens", "output_tokens", "total_tokens")
                },
            }
        finally:
            self.lock.release()
