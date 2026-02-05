from typing import Any, Dict

from fastapi import Request

from freva_gpt.services.mcp.mcp_manager import McpManager
from freva_gpt.services.storage.helpers import get_mongodb_uri

# DEPRECATED


def session_key_from_request(request: Request) -> Any:
    """
    Choose a stable key per conversation/thread. Replace this with your real ID.
    """
    # Example fallbacks: header → query → remote addr
    return (
        request.headers.get("x-thread-id")
        or request.query_params.get("thread_id")
        or request.client.host  # last resort
    )


async def call_rag(
    request: Request,
    question: str,
    resource: str,
) -> Dict[str, Any]:
    """
    Calls the RAG MCP tool. For now we forward vault/rest headers on EVERY call
    (your header gate requires them each time).
    """
    mgr: McpManager = request.app.state.mcp

    vault_url = request.headers.get("x-freva-vault-url")
    mongodb_uri = get_mongodb_uri(vault_url)

    auth = request.headers.get("Authorization")

    extra_headers = {
        "Authorization": auth if auth else None,
        "mongo-uri": mongodb_uri,
    }

    res = mgr.call_tool(
        "rag",
        name="get_context_from_resources",
        arguments={
            "question": question,
            "resources_to_retrieve_from": resource,
        },
        extra_headers=extra_headers,
    )
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "raw": res.get("raw")}
    return {"ok": True, "result": res.get("result")}


async def call_code(
    request: Request,
    code: str,
) -> Dict[str, Any]:
    """
    Calls the code interpreter. We forward Authorization if present.
    """
    mgr: McpManager = request.app.state.mcp

    auth = request.headers.get("Authorization")
    extra_headers = {"Authorization": auth} if auth else None

    res = mgr.call_tool(
        "code",
        name="code_interpreter",
        arguments={"code": code},
        extra_headers=extra_headers,
    )
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "raw": res.get("raw")}
    return {"ok": True, "result": res.get("result")}
