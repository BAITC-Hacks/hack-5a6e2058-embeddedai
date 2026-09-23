"""Local-first API. Unpredictable run IDs are capability links for uploaded runs."""

import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import storage as store
from .assistant import AnalystAssistant, AssistantError
from .demo import create_demo
from .investigation import node_evidence, resilience
from .loader import DataError
from .pipeline import EXPORTS, ROOT, analyze, input_fingerprints, read_rules, write_result
from .queue import node_queue
from .robustness import analyze_robustness
from .roles import LABELS

MAX_FILE_BYTES = 10 * 1024 * 1024


def create_app(initial_data: Path | None = None, storage: Path | None = None) -> FastAPI:
    app = FastAPI(title="Граф денег", version="0.8.0")
    directory = (storage or Path(os.getenv("MONEY_GRAPH_STORAGE", ROOT / "var/runs"))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    gate = threading.Lock()
    rules = read_rules()
    snapshots = store.ResultCache(directory, rules)
    assistant = AnalystAssistant()
    robustness_gate = threading.Lock()
    robustness_cache: tuple[dict[str, Any], dict[str, Any]] | None = None

    def calculate(source: Path, synthetic: bool) -> dict[str, Any]:
        # A running process uses one configuration for both new and restored runs.
        with tempfile.TemporaryDirectory(prefix=".rules-", dir=directory) as temp:
            snapshot = Path(temp) / "rules.json"
            snapshot.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
            result = analyze(source, snapshot)
        result["report"]["synthetic"] = synthetic
        return result

    initial = store.read_bootstrap(directory)
    current = False
    demo = initial_data is None
    if initial and initial["synthetic"] == demo:
        try:
            saved = snapshots.get(initial["run_id"])
            current = (
                saved["report"].get("rules") == rules
                and saved["report"].get("rules_version") == rules["version"]
                and saved["report"].get("synthetic") == demo
            )
            if current and initial_data is not None:
                digests = input_fingerprints(initial_data)
                current = saved["report"].get("input_sha256") == digests
        except (store.MissingRun, store.CorruptRun, store.ObsoleteRun, OSError):
            current = False
    if not current:
        if demo:
            with tempfile.TemporaryDirectory() as temp:
                create_demo(Path(temp))
                result = calculate(Path(temp), synthetic=True)
        else:
            assert initial_data is not None
            result = calculate(initial_data, synthetic=False)
        run_id = secrets.token_hex(16)
        write_result(result, directory / run_id)
        initial = {"run_id": run_id, "synthetic": demo}
        store.write_bootstrap(directory, initial)
    assert initial is not None
    bootstrap = initial

    def result_for(run_id: str) -> dict[str, Any]:
        try:
            result = snapshots.get(run_id)
        except store.MissingRun as exc:
            raise HTTPException(
                404, "Расчёт не найден или срок хранения истёк; загрузите данные повторно"
            ) from exc
        except store.ObsoleteRun as exc:
            raise HTTPException(
                409,
                "Конфигурация анализа обновлена. Загрузите файлы повторно; прежние CSV доступны по сохранённым ссылкам выгрузки",
            ) from exc
        except store.CorruptRun as exc:
            raise HTTPException(
                503,
                "Сохранённый расчёт повреждён. Скачайте доступные CSV или загрузите файлы повторно",
            ) from exc
        return result

    @app.middleware("http")
    async def response_headers(request: Any, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.8.0"}

    @app.get("/api/bootstrap")
    def initial_run() -> dict[str, Any]:
        return {
            **bootstrap,
            "labels": LABELS,
            "upload_limit_mb": 10,
            "llm_enabled": assistant.enabled,
        }

    @app.get("/api/assistant/status")
    def assistant_status() -> dict[str, Any]:
        return assistant.status()

    @app.post("/api/runs/{run_id}/assistant")
    def ask_assistant(
        run_id: str,
        body: Annotated[Any, Body()],
        access_token: Annotated[str, Header(alias="X-Assistant-Token")] = "",
    ) -> dict[str, Any]:
        try:
            return assistant.ask(result_for(run_id), body, access_token, run_id=run_id)
        except AssistantError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/api/analyze")
    def upload(
        nodes: Annotated[UploadFile, File()],
        edges: Annotated[UploadFile, File()],
        transactions: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        if not gate.acquire(blocking=False):
            raise HTTPException(429, "Расчёт уже выполняется. Повторите через несколько секунд")
        try:
            with tempfile.TemporaryDirectory(dir=directory) as temp:
                source = Path(temp)
                for name, file in (
                    ("nodes", nodes),
                    ("edges", edges),
                    ("transactions", transactions),
                ):
                    data = file.file.read(MAX_FILE_BYTES + 1)
                    if len(data) > MAX_FILE_BYTES:
                        raise HTTPException(413, f"{name}: файл больше 10 МБ")
                    (source / f"{name}.parquet").write_bytes(data)
                result = calculate(source, synthetic=False)
            run_id = secrets.token_hex(16)
            write_result(result, directory / run_id)
            store.prune(directory, bootstrap["run_id"], run_id)
            return {"run_id": run_id, "report": result["report"], "synthetic": False}
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            gate.release()
            for file in (nodes, edges, transactions):
                file.file.close()

    @app.get("/api/runs/{run_id}")
    def report(run_id: str) -> dict[str, Any]:
        return result_for(run_id)["report"]

    @app.get("/api/runs/{run_id}/top")
    def top(run_id: str) -> list[dict[str, Any]]:
        return result_for(run_id)["top"]

    @app.get("/api/runs/{run_id}/clusters")
    def clusters(run_id: str) -> list[dict[str, Any]]:
        return result_for(run_id)["clusters"]

    @app.get("/api/runs/{run_id}/nodes")
    def queue(
        run_id: str,
        role: str | None = None,
        cluster: int | None = None,
        depth: Annotated[int | None, Query(ge=0, le=4)] = None,
        seeds: bool = False,
        search: Annotated[str, Query(max_length=30)] = "",
        sort: str = "rank",
        order: str = "asc",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        try:
            return node_queue(
                result_for(run_id),
                role=role,
                cluster=cluster,
                depth=depth,
                seeds=seeds,
                search=search.strip(),
                sort=sort,
                order=order,
                offset=offset,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/runs/{run_id}/robustness")
    def robustness(run_id: str) -> dict[str, Any]:
        nonlocal robustness_cache
        result = result_for(run_id)
        if not robustness_gate.acquire(blocking=False):
            raise HTTPException(
                429, "Диагностика уже выполняется. Повторите через несколько секунд"
            )
        try:
            if robustness_cache is not None and robustness_cache[0] is result:
                return robustness_cache[1]
            diagnostic = analyze_robustness(result)
            robustness_cache = (result, diagnostic)
            return diagnostic
        except DataError:
            raise HTTPException(
                503, "Не удалось проверить устойчивость расчёта. Повторите анализ исходных файлов"
            ) from None
        finally:
            robustness_gate.release()

    @app.get("/api/runs/{run_id}/community-graph")
    def community_graph(run_id: str) -> dict[str, Any]:
        result = result_for(run_id)
        return {"nodes": result["clusters"], "edges": result["cluster_edges"]}

    @app.get("/api/runs/{run_id}/resilience")
    def simulate(run_id: str, count: Annotated[int, Query(ge=1, le=20)] = 5) -> dict[str, Any]:
        return resilience(result_for(run_id), count)

    @app.get("/api/runs/{run_id}/nodes/{gid}/investigation")
    def investigate(run_id: str, gid: str) -> dict[str, Any]:
        result = result_for(run_id)
        if gid not in {r["gid"] for r in result["nodes"]}:
            raise HTTPException(404, "Такого gid нет в наборе")
        return node_evidence(result, gid)

    @app.get("/api/runs/{run_id}/nodes/{gid}")
    def node(run_id: str, gid: str) -> dict[str, Any]:
        result = result_for(run_id)
        row = next((r for r in result["nodes"] if r["gid"] == gid), None)
        if row is None:
            raise HTTPException(404, "Такого gid нет в наборе. Вставьте полный идентификатор")
        return {
            **row,
            "incoming": [e for e in result["edges"] if e["dst"] == gid and e["src"] != gid],
            "outgoing": [e for e in result["edges"] if e["src"] == gid and e["dst"] != gid],
            "self_transfers": [e for e in result["edges"] if e["src"] == gid and e["dst"] == gid],
        }

    @app.get("/api/runs/{run_id}/graph")
    def graph(
        run_id: str,
        gid: str | None = None,
        cluster: int | None = None,
        role: str | None = None,
        depth: int | None = None,
        seeds: bool = False,
        radius: Annotated[int, Query(ge=1, le=2)] = 1,
        limit: Annotated[int, Query(ge=1, le=10000)] = 350,
    ) -> dict[str, Any]:
        result = result_for(run_id)
        rows = result["nodes"]
        candidates = {r["gid"] for r in rows}
        if gid:
            if gid not in candidates:
                raise HTTPException(404, "Такого gid нет в наборе")
            candidates = {gid}
            for _ in range(radius):
                neighbors = {e["src"] for e in result["edges"] if e["dst"] in candidates}
                neighbors.update(e["dst"] for e in result["edges"] if e["src"] in candidates)
                candidates |= neighbors
        else:
            candidates = {
                r["gid"]
                for r in rows
                if (cluster is None or r["cluster_id"] == cluster)
                and (not role or r["role"] == role)
                and (depth is None or r["depth"] == depth)
                and (not seeds or r["is_seed"])
            }
        selected = sorted(
            [r for r in rows if r["gid"] in candidates],
            key=lambda r: (r["gid"] != gid, -r["priority_score"], int(r["gid"])),
        )[:limit]
        included = {r["gid"] for r in selected}
        fields = (
            "gid",
            "role",
            "role_score",
            "priority_score",
            "cluster_id",
            "depth",
            "is_seed",
            "boundary_censored",
            "isolated",
        )
        return {
            "nodes": [{k: row[k] for k in fields} for row in selected],
            "edges": [e for e in result["edges"] if e["src"] in included and e["dst"] in included],
            "total": len(rows),
            "matched": len(candidates),
            "shown": len(selected),
        }

    @app.get("/api/runs/{run_id}/exports/{name}")
    def export(run_id: str, name: str) -> Response:
        if name not in (*EXPORTS, "report.html"):
            raise HTTPException(404, "Неизвестная выгрузка")
        try:
            path = store.run_path(directory, run_id) / name
            if name == "report.html" and not path.exists():
                # Older, still-valid runs predate the offline report. No migration required.
                from .offline_report import render_report

                content = render_report(result_for(run_id)).encode("utf-8")
            else:
                content = path.read_bytes()
        except (store.MissingRun, FileNotFoundError) as exc:
            raise HTTPException(404, "Выгрузка не найдена или срок хранения истёк") from exc
        except OSError as exc:
            raise HTTPException(503, "Не удалось прочитать выгрузку; повторите позже") from exc
        return Response(
            content,
            media_type="text/html; charset=utf-8"
            if name == "report.html"
            else "text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    static = ROOT / "web/dist"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app
