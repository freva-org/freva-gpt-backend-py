import os

from fastmcp import FastMCP

from src.tools.header_gate import make_header_gate
from src.tools.server_auth import jwt_verifier

from src.core.logging_setup import configure_logging
from openai import OpenAI

logger = configure_logging(__name__, named_log="web_search_server")

OPENAI_API_KEY: str = os.getenv("FREVAGPT_OPENAI_API_KEY")

_disable_auth = os.getenv("FREVAGPT_MCP_DISABLE_AUTH", "0").lower() in {"1","true","yes"}
mcp = FastMCP("web-search-server", auth=None if _disable_auth else jwt_verifier)

# ── Config ───────────────────────────────────────────────────────────────────
WEB_SEARCH_MODEL="gpt-4o"
ALLOWED_DOMAINS=[
    "docs.dkrz.de",
    "docs.icon-model.org",
    ]
# ─────────────────────────────────────────────────────────────────────────────

client = OpenAI(api_key=OPENAI_API_KEY)

@mcp.tool()
def web_search(query: str) -> str:
    """
    Calls a web-search agent to access DKRZ/HPC and ICON model documentation website.
    Args:
        query (str): The user's (or LLMs) query.
    Returns:
        str: Relevant context extracted from web-page.
    """
    logger.info("Searching for DKRZ/HPC- or ICON-related context in documentation "\
                f"for query: {query}")
    prompt = (
        "You are a web-search agent that can search documentations for ICON model "\
        "and DKRZ/HPC. Use the documentation websites for searching and creating "\
        "answers. Make sure the information provided is accurate and up-to-date. "\
        "DKRZ/HPC doc 'https://docs.dkrz.de/search.html?q=SEARCHTERM1+SEARCHTERM2'. "\
        "ICON doc 'https://docs.icon-model.org/search.html?q=SEARCHTERM1+SEARCHTERM2'. "\
        "Use SEARCHTEAM 1 and 2 to find relevant information. Only answer questions "\
        "if claims can be supported by web citations. Include inline citations for "\
        f"URLs found in the web search results.\n\n User query:\n{(query or '')}"
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
        logger.info(f"Succesfully completed web search with query {query}.\n")
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
    host = os.getenv("FREVAGPT_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("FREVAGPT_MCP_PORT", "8052"))
    path = os.getenv("FREVAGPT_MCP_PATH", "/mcp")  # standard path

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
    uvicorn.run(wrapped_app, host=host, port=port, ws="websockets-sansio",)
