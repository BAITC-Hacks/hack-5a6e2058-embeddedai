"""Confirm the built frontend is actually served by the production app."""
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from money_graph.api import create_app

with TemporaryDirectory() as tmp:
    client = TestClient(create_app(storage=Path(tmp)))
    assert client.get('/health').status_code == 200
    page = client.get('/')
    assert page.status_code == 200 and '/assets/' in page.text
    run = client.get('/api/bootstrap').json()['run_id']
    report = client.get(f'/api/runs/{run}').json()
    assert report['synthetic'] and report['n_nodes'] > 0
    for name in ['nodes_roles.csv', 'clusters.csv', 'top_nodes.csv']:
        assert client.get(f'/api/runs/{run}/exports/{name}').status_code == 200
print('Built frontend + production API + CSV smoke: OK')
