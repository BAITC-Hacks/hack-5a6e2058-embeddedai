"""Local-first API. Unpredictable run IDs are capability links for uploaded runs."""

import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .demo import create_demo
from .loader import DataError
from .pipeline import EXPORTS, ROOT, analyze, write_result
from .roles import LABELS

MAX_FILE_BYTES = 10 * 1024 * 1024


def create_app(initial_data: Path | None = None, storage: Path | None = None) -> FastAPI:
    app = FastAPI(title="Граф денег", version="0.1.0")
    directory = (storage or Path(os.getenv("MONEY_GRAPH_STORAGE", ROOT / "var/runs"))).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    gate = threading.Lock()
    bootstrap_file = directory / "bootstrap.json"
    if not bootstrap_file.exists() or initial_data is not None:
        demo = initial_data is None
        if demo:
            with tempfile.TemporaryDirectory() as temp:
                create_demo(Path(temp))
                result = analyze(Path(temp))
        else:
            result = analyze(initial_data)  # type: ignore[arg-type]
        run_id = secrets.token_hex(16)
        write_result(result, directory / run_id)
        bootstrap_file.write_text(json.dumps({"run_id": run_id, "synthetic": demo}))
    bootstrap = json.loads(bootstrap_file.read_text())

    def result_for(run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise HTTPException(404, "Расчёт не найден")
        path = directory / run_id / "result.json"
        if not path.is_file():
            raise HTTPException(
                404, "Расчёт не найден или срок хранения истёк; загрузите данные повторно"
            )
        return json.loads(path.read_text())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/bootstrap")
    def initial() -> dict[str, Any]:
        return {**bootstrap, "labels": LABELS, "upload_limit_mb": 10, "llm_enabled": False}

    @app.post("/api/analyze")
    def upload(
        nodes: Annotated[UploadFile, File()],
        edges: Annotated[UploadFile, File()],
        transactions: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        if not gate.acquire(blocking=False):
            raise HTTPException(429, "Расчёт уже выполняется. Повторите через несколько секунд")
        try:
            now = time.time()
            # Retain the demo and bound uploaded generations by age and count.
            saved = sorted(
                [
                    p
                    for p in directory.iterdir()
                    if p.is_dir()
                    and re.fullmatch(r"[a-f0-9]{32}", p.name)
                    and p.name != bootstrap["run_id"]
                ],
                key=lambda p: p.stat().st_mtime,
            )
            for index, path in enumerate(saved):
                if now - path.stat().st_mtime > 86400 or index < len(saved) - 19:
                    shutil.rmtree(path)
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
                result = analyze(source)
            run_id = secrets.token_hex(16)
            write_result(result, directory / run_id)
            return {"run_id": run_id, "report": result["report"], "synthetic": False}
        except DataError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            gate.release()
            for file in (nodes, edges, transactions):
                file.file.close()

    @app.get("/api/runs/{run_id}")
    def report(run_id: str) -> dict[str, Any]:
        return {
            **result_for(run_id)["report"],
            "synthetic": bootstrap["synthetic"] if run_id == bootstrap["run_id"] else False,
        }

    @app.get("/api/runs/{run_id}/top")
    def top(run_id: str) -> list[dict[str, Any]]:
        return result_for(run_id)["top"]

    @app.get("/api/runs/{run_id}/clusters")
    def clusters(run_id: str) -> list[dict[str, Any]]:
        return result_for(run_id)["clusters"]

    @app.get("/api/runs/{run_id}/nodes/{gid}")
    def node(run_id: str, gid: str) -> dict[str, Any]:
        result = result_for(run_id)
        row = next((r for r in result["nodes"] if r["gid"] == gid), None)
        if row is None:
            raise HTTPException(404, "Такого gid нет в наборе. Вставьте полный идентификатор")
        return {
            **row,
            "incoming": [e for e in result["edges"] if e["dst"] == gid],
            "outgoing": [e for e in result["edges"] if e["src"] == gid],
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
    def export(run_id: str, name: str) -> FileResponse:
        result_for(run_id)
        if name not in EXPORTS:
            raise HTTPException(404, "Неизвестная выгрузка")
        return FileResponse(
            directory / run_id / name, media_type="text/csv; charset=utf-8", filename=name
        )

    static = ROOT / "web/dist"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app
