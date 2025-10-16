from fastapi import APIRouter, Request
from src.tools.tool_calls import call_rag, call_code

router = APIRouter()

@router.post("/mcp/rag")
async def rag_endpoint(request: Request, question: str, resource: str, thread_id: str):
    txt = await call_rag(request, question, resource, thread_id)
    return {"ok": True, "text": txt}

@router.post("/mcp/code")
async def code_endpoint(request: Request, code: str, thread_id: str):
    txt = await call_code(request, code, thread_id)
    return {"ok": True, "text": txt}
