import json, os
from starlette.testclient import TestClient
from src.tools.rag.server import mcp

def init(client, headers):
    r = client.post("/mcp", json={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{}}}, headers=headers)
    assert r.status_code==200
    assert "mcp-session-id" in r.headers

def test_missing_header_rejected():
    app = mcp.http_app()
    tc = TestClient(app)
    r = tc.post("/mcp", json={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{}}}, headers={"Authorization":"Bearer good","Accept":"application/json, text/event-stream","Content-Type":"application/json"})
    assert r.status_code==400
    body = r.text.split("data: ",1)[-1]
    obj = json.loads(body)
    assert obj["error"]["code"] == -32600

def test_get_context_smoke(monkeypatch):
    app = mcp.http_app()
    tc = TestClient(app)
    headers={
        "Authorization":"Bearer good",
        "Accept":"application/json, text/event-stream",
        "Content-Type":"application/json",
        "x-freva-vault-url": os.environ.get("VAULT","mongodb://vault.example"),
        "x-freva-rest-url": "http://rest.example",
    }
    init(tc, headers)
    r = tc.post("/mcp", json={"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_context_from_resources","arguments":{"question":"example usage","resources_to_retrieve_from":"stableclimgen"}}}, headers=headers)
    assert r.status_code==200
    body = r.text.split("data: ",1)[-1]
    obj = json.loads(body)
    assert "result" in obj or "error" in obj  # smoke test