"""Validate UI context and retain small, isolated, ephemeral conversations."""

import hashlib
import json
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .assistant_prompts import VIEWS
from .roles import LABELS

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


def validate_request(body: Any, known: set[str]) -> tuple[str, list[str]]:
    required = {"question", "selected_gids"}
    if (
        not isinstance(body, dict)
        or not required <= set(body)
        or set(body) - required - {"context", "conversation_id"}
    ):
        raise AssistantError(422, "Нужны question и selected_gids; допустимы context и conversation_id")
    question, selected = body["question"], body["selected_gids"]
    if not isinstance(question, str) or not 2 <= len(question.strip()) <= 2000:
        raise AssistantError(422, "Вопрос должен содержать от 2 до 2000 символов")
    if not isinstance(selected, list) or len(selected) > 20 or any(not valid_gid(g) for g in selected):
        raise AssistantError(422, "Выберите до 20 узлов; gid должны быть строками int64")
    if any(g not in known for g in selected):
        raise AssistantError(422, "Один из выбранных gid отсутствует в текущем графе")
    cid = body.get("conversation_id")
    if cid is not None and (not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", cid)):
        raise AssistantError(422, "Некорректный идентификатор диалога. Начните новый диалог")
    return question.strip(), list(dict.fromkeys(selected))


def validate_context(raw: Any, result: dict[str, Any], known: set[str]) -> dict[str, Any]:
    if raw is None:
        return {"active_gid": None, "active_tab": "graph", "filters": {"role": None, "cluster": None, "depth": None, "seeds": False}}
    bad = "Некорректный контекст экрана. Обновите страницу и повторите"
    if not isinstance(raw, dict) or set(raw) != {"active_gid", "active_tab", "filters"}:
        raise AssistantError(422, bad)
    gid = raw["active_gid"]
    if gid is not None and (not valid_gid(gid) or gid not in known):
        raise AssistantError(422, "Открытый узел отсутствует в текущем графе")
    if raw["active_tab"] not in VIEWS:
        raise AssistantError(422, bad)
    filters = raw["filters"]
    if not isinstance(filters, dict) or set(filters) != {"role", "cluster", "depth", "seeds"}:
        raise AssistantError(422, bad)
    if filters["role"] not in [None, "", *LABELS] or type(filters["seeds"]) is not bool:
        raise AssistantError(422, bad)
    depth, cluster = filters["depth"], filters["cluster"]
    if depth is not None and (type(depth) is not int or not 0 <= depth <= 4):
        raise AssistantError(422, bad)
    if cluster is not None and (type(cluster) is not int or cluster not in {n["cluster_id"] for n in result["nodes"]}):
        raise AssistantError(422, bad)
    return {**raw, "filters": {**filters, "role": filters["role"] or None}}


@dataclass
class Conversation:
    run_id: str
    fingerprint: str
    updated: float
    history: list[dict[str, Any]] = field(default_factory=list)


class Conversations:
    """Accessed under the assistant request lock; no files or provider-side history."""

    TTL = 1800
    MAX_SESSIONS = 50
    MAX_HISTORY_CHARS = 20_000

    def __init__(self) -> None:
        self.sessions: OrderedDict[str, Conversation] = OrderedDict()

    def get(self, cid: str | None, run_id: str, report: dict[str, Any]) -> tuple[str, Conversation]:
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if now - session.updated >= self.TTL:
                del self.sessions[key]
        fingerprint = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
        if cid is not None:
            session = self.sessions.get(cid)
            if session is None or session.run_id != run_id or session.fingerprint != fingerprint:
                raise AssistantError(409, "Диалог устарел или относится к другому набору. Начните новый диалог и повторите вопрос")
            return cid, session
        return secrets.token_urlsafe(24), Conversation(run_id, fingerprint, now)

    def save(self, cid: str, session: Conversation, question: str, answer: dict[str, Any]) -> None:
        # Keep verified facts separate from generated prose; both fit a hard history budget.
        facts = answer["facts"][:20]
        while len(json.dumps(facts, ensure_ascii=False)) > 7000:
            facts.pop()
        turn = {
            "question": question,
            "answer": answer["answer"],
            "gids": [row["gid"] for row in answer["nodes"]],
            "verified_facts": facts,
            "queries": answer["query"],
        }
        session.history = (session.history + [turn])[-4:]
        while len(json.dumps(session.history, ensure_ascii=False)) > self.MAX_HISTORY_CHARS:
            if len(session.history) > 1:
                session.history.pop(0)
            else:
                session.history[0]["verified_facts"] = []
                session.history[0]["queries"] = {}
                break
        session.updated = time.monotonic()
        self.sessions[cid] = session
        self.sessions.move_to_end(cid)
        while len(self.sessions) > self.MAX_SESSIONS:
            self.sessions.popitem(last=False)
