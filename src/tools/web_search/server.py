import os

from fastmcp import FastMCP

from src.tools.header_gate import make_header_gate
from src.tools.server_auth import jwt_verifier

from src.core.logging_setup import configure_logging
from openai import OpenAI

logger = configure_logging(__name__, named_log="web-search-server")

OPENAI_API_KEY: str = os.getenv("FREVAGPT_OPENAI_API_KEY")

_disable_auth = os.getenv("FREVAGPT_MCP_DISABLE_AUTH", "0").lower() in {"1","true","yes"}  # for local testing
mcp = FastMCP("web-search-server", auth=None if _disable_auth else jwt_verifier)

# ── Config ───────────────────────────────────────────────────────────────────
WEB_SEARCH_MODEL="gpt-4o"
ALLOWED_DOMAINS=[
    "docs.dkrz.de",
    ]
# ─────────────────────────────────────────────────────────────────────────────

client = OpenAI(api_key=OPENAI_API_KEY)

@mcp.tool()
def web_search(query: str) -> str:
    """
    Call a web-search agent to access HPC and DKRZ documentation website
    Args:
        query (str): The user's (or LLMs) query.
    Returns:
        str: Relevant context extracted from web-page.
    """
    logger.info(f"Searching doc.dkrz.de for DKRZ/HPC-related context in documentation for query: {query}")
    prompt = (
        "Use the DKRZ documentation website 'https://docs.dkrz.de/search.html?q=' for searching "\
        "and creating answers, ensuring the information provided is accurate and up-to-date."\
        "'https://docs.dkrz.de/search.html?q=SEARCHTERM1+SEARCHTERM2' use SEARCHTEAM 1 and 2"\
        "to find relevant information. Only answer questions if claims can be supported by web citations.\n\n"
        f"User question:\n{(query or '')}"
    )
    kwargs = {
        "model": WEB_SEARCH_MODEL, 
        "input": [{"role": "user", "content": prompt}], 
        "stream": False,
        "tool_choice": "auto",
        "tools": [
            {
                "type": "web_search",
                "filters": {
                    "allowed_domains": ALLOWED_DOMAINS
                }
            }
        ],
        "include": ["web_search_call.action.sources"],
    }

    try:
        resp = client.responses.create(**kwargs)
        return resp.output_text
    except Exception as e:
        logger.warning("Web-search failed due to error: %s", e)
        return 


def debug():
    question = "How do I submit a job to the DKRZ HPC?"
    resp = web_search(question)
    print(resp)

    
if __name__ == "__main__":
    # Configure Streamable HTTP transport 
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8052"))
    path = os.getenv("MCP_PATH", "/mcp")  # standard path

    logger.info("Starting Web-Search MCP server on %s:%s%s (auth=%s)",
                host, port, path, "off" if _disable_auth else "on")

    # Start the MCP server using Streamable HTTP transport
    wrapped_app = make_header_gate(
        mcp.http_app(),
        ctx_list=[],
        header_name_list=[],
        logger=logger,       
        mcp_path=path,  
    )

    import uvicorn
    uvicorn.run(wrapped_app, host=host, port=port)
