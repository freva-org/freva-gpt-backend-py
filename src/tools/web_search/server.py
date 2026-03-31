import os

from fastmcp import FastMCP

from src.tools.header_gate import make_header_gate

from src.core.logging_setup import configure_logging
from openai import OpenAI

logger = configure_logging(__name__, named_log="web_search_server")

OPENAI_API_KEY: str = os.getenv("FREVAGPT_OPENAI_API_KEY")

mcp = FastMCP("web-search-server")


# ── Config ───────────────────────────────────────────────────────────────────
WEB_SEARCH_MODEL = "gpt-4.1"
ALLOWED_DOMAINS = [
    "docs.dkrz.de",
    "docs.icon-model.org",
]

MKEXP_PDF_URL = "https://gitlab.dkrz.de/esmenv/mkexp/-/raw/master/doc/mkexp.pdf"

HOST = os.getenv("FREVAGPT_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("FREVAGPT_MCP_PORT", "8052"))
PATH = os.getenv("FREVAGPT_MCP_PATH", "/mcp")  # standard path


# ─── App ────────────────────────────────────────────────────────────────────

logger.info("Starting Web-Search MCP server on %s:%s%s", HOST, PORT, PATH)

# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[],
    header_name_list=[],
    logger=logger,
    mcp_path=PATH,
)

client = OpenAI(api_key=OPENAI_API_KEY)


# ─── Tool ───────────────────────────────────────────────────────────────────

def should_attach_mkexp_pdf(query: str) -> bool:
    q = (query or "").lower()
    keywords = [
        "mkexp",
        "experiment",
        "set up an experiment",
        "setup an experiment",
        "make an experiment",
        "run_start",
        # mkexp-specific terminology
        ".config",
        "experiment config",
        "cpexp",
        "diffexp",
        "namelist",
        "master namelist",
        "yaml namelist",
        "fortran namelist",
        "runscript",
        "reinitialization",
    ]
    return any(k in q for k in keywords)



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
    system_prompt = (
        "You are a web-search agent that can search documentations for DKRZ/HPC, "
        "ICON model and mkexp toolbox. Use the documentation websites for searching "\
        "and creating answers. Make sure the information provided is accurate and up-to-date. "\
        "DKRZ/HPC doc 'https://docs.dkrz.de/search.html?q=SEARCHTERM1+SEARCHTERM2'. "\
        "ICON doc 'https://docs.icon-model.org/search.html?q=SEARCHTERM1+SEARCHTERM2'. "\
        "mkexp toolbox 'https://gitlab.dkrz.de/esmenv/mkexp/-/raw/master/doc/mkexp.pdf'. "\
        "For DKRZ/HPC and ICON doxs, use SEARCHTEAM 1 and 2 to find relevant information. "\
        "When asked about mkexp or seting up an experiment, consult ICON docs AND mkexp toolbox docs."
        "Only answer questions if claims can be supported by web citations. Include inline citations for "\
        "URLs found in the web search results."
    )

    user_content = [
        {"type": "input_text", "text": query or ""}
    ]

    if should_attach_mkexp_pdf(query):
        user_content.append(
            {
                "type": "input_file",
                "file_url": MKEXP_PDF_URL,
            }
        )


    kwargs = {
        "model": WEB_SEARCH_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "tool_choice": "auto",
        "tools": [
            {"type": "web_search", "filters": {"allowed_domains": ALLOWED_DOMAINS}}
        ],
        "include": ["web_search_call.action.sources"],
    }
    logger.info(kwargs)
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
