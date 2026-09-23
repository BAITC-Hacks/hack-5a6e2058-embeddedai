"""Isolated synthetic server for reproducible browser acceptance checks."""
import tempfile
from pathlib import Path

import uvicorn

from money_graph.api import create_app
from money_graph.demo import create_demo
from money_graph.pipeline import ROOT

create_demo(ROOT / "var/e2e-data")
with tempfile.TemporaryDirectory(prefix="money-graph-e2e-") as temp:
    uvicorn.run(create_app(storage=Path(temp)), host="127.0.0.1", port=3037)
