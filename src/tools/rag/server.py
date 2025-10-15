import os

import requests
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy

from src.variables import *
from src.servers.rag.helpers import *
from src.servers.rag.document_loaders import CustomDirectoryLoader
from src.servers.rag.text_splitters import CustomDocumentSplitter

from src.logging_setup import configure_logging

logger = configure_logging()


ISSUER   = os.getenv("OIDC_ISSUER", "https://www.freva.dkrz.de/api/freva-nextgen/")  # TODO: check this
AUDIENCE = os.getenv("OIDC_AUDIENCE", "mcp-servers")

# Discover JWKS from OIDC config
disc = requests.get(ISSUER.rstrip("/") + "/.well-known/openid-configuration", timeout=10).json()
JWKS_URI = disc["jwks_uri"]

token_verifier = JWTVerifier(
    jwks_uri=JWKS_URI,
    issuer=ISSUER,          # must match token `iss` exactly
    audience=AUDIENCE       # must match token `aud` (string or in array)
)

# # Create the OAuth proxy
# auth = OAuthProxy(
#     # Provider's OAuth endpoints (from their documentation)
#     upstream_authorization_endpoint="https://freva-keycloak.cloud.dkrz.de/realms/Freva/protocol/openid-connect/auth",
#     upstream_token_endpoint="https://freva-keycloak.cloud.dkrz.de/realms/Freva/protocol/openid-connect/token",

#     # Your registered app credentials
#     upstream_client_id="freva",
#     upstream_client_secret="your-client-secret",

#     # Token validation (see Token Verification guide)
#     token_verifier=token_verifier,

#     # Your FastMCP server's public URL
#     base_url="http://localhost:8050",

#     # Optional: customize the callback path (default is "/auth/callback")
#     redirect_path="/callback",
# )

mcp = FastMCP("rag_server", auth=token_verifier)

PROXY_URL = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
API_KEY   = os.getenv("LITELLM_API_KEY", "dummy")  # set on the proxy


def get_embedding(text):
    """Get embedding for a given text"""
    payload = {
        "model": EMBEDDING_MODEL,  # or a proxy alias you define
        "input": text,
        "temperature": 0.2,
        }
    r = requests.post(
            f"{PROXY_URL}/v1/embeddings",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=payload,
            timeout=60,
            )
    response = r.json()
    # response = litellm.embedding(
    #     input=text,
    #     model=EMBEDDING_MODEL, # os.getenv("EMBEDDING_MODEL"),  # Model to use for embeddings
    #     temperature=0.2,  # Temperature for the model
    #     api_base= OLLAMA_BASE_URL,  # Base URL for the API
    # )

    if response.data and len(response.data) != 0:
        embedding = response.data[0]['embedding']
        return embedding
    elif not response.data:
        raise ValueError("No embedding data returned from the model.")
    if not isinstance(response.data[0], dict) or 'embedding' not in response.data[0]:
        raise ValueError("Embedding data is not in the expected format.")


def create_db_entry_for_document(document):
    entry = {
        "resource_type": "example" if ".json" in document.metadata.get("source") else "document", 
        "resource_name": document.metadata.get("resource_name"), 
        "document": document.metadata.get("source"),
        "chunk_id": document.metadata.get("chunk_id"),
        "file_hash": document.metadata.get("file_hash"),
        "content": document.page_content,
        "embedded_content": document.metadata["embedded_content"],
        "embedding": get_embedding(document.metadata["embedded_content"]),
        }
    return entry


def store_documents_in_mongodb(documents):
    """Create and store embeddings for the provided documents."""
    new_documents = get_new_or_changes_documents(documents, DB_COLLECTION)
    new_entries = []

    for d in new_documents:
        entry = create_db_entry_for_document(d)
        new_entries.append(entry)

    # Insert new embeddings
    if new_entries:
        logger.info(f"Inserting {len(new_entries)} new embeddings into MongoDB")
        DB_COLLECTION.insert_many(new_entries)


def get_query_results(query: str, resource_name):
    """Gets results from a vector search query."""
    add_vector_search_index_to_db(DB_COLLECTION, EMBEDDING_LENGTH)

    logger.info(f"Searching for query: {query}")
    query_embedding = get_embedding(query)
    query_results = []

    src_types = DB_COLLECTION.distinct("resource_type")
    for src_t in src_types:
        pipeline = [
        {
                "$vectorSearch": {
                "index": "vector_index",
                "queryVector": query_embedding,
                "filter": {
                    "$and": [
                        { "resource_type": src_t },
                        { "resource_name": resource_name} 
                        ] 
                },
                "path": "embedding",
                "numCandidates": 15,
                "limit": 3
                }
        }, {
                "$project": {
                "content": 1,
                "resource_type": 1,
                "resource_name":1,
                "document":1,
                "chunk_id":1,
                'score': {
                    '$meta': 'vectorSearchScore'
                    }
            }
        }
        ]

        query_results.append(list(DB_COLLECTION.aggregate(pipeline)))

    if query_results:
        return postprocessing_query_result(query_results)
    else:
        logger.info("No results found for the query.")
        return "No content found."


@mcp.tool()
def get_context_from_resources(question: str, resources_to_retrieve_from: str) -> str:
    """
    Search Python package/library documentation and examples to find relevant context.
    Args:
        question (str): The user's question.
        resources_to_retrieve_from (str): The name of the library to search the documentation for. It should be one of the folder names in RESOURCE_DIR.
    Returns:
        str: Relevant context extracted from the library documentation.
    """
    logger.info(f"Searching for context in {resources_to_retrieve_from} documentation for question: {question}")
    if resources_to_retrieve_from not in AVAILABLE_RESOURCES:
        logger.error(f"Library '{resources_to_retrieve_from}' is not supported.")
        return f"Library '{resources_to_retrieve_from}' is not supported."

    if CLEAR_EMBEDDINGS == "True":
        clear_embeddings_collection(DB_COLLECTION)

    dir_loader = CustomDirectoryLoader(os.path.join(RESOURCE_DIR, resources_to_retrieve_from))
    documents = dir_loader.load()
    doc_splitter = CustomDocumentSplitter(documents, chunk_size=500, chunk_overlap=50, separators="\n\n")
    chunked_documents = doc_splitter.split()

    store_documents_in_mongodb(chunked_documents)

    context = get_query_results(question, resources_to_retrieve_from)

    return context

def debug():
    resources_to_retrieve_from = "stableclimgen"
    question = "Get global temperature data from February 2nd 1940"

    dir_loader = CustomDirectoryLoader(os.path.join(RESOURCE_DIR, resources_to_retrieve_from))
    documents = dir_loader.load()
    doc_splitter = CustomDocumentSplitter(documents, chunk_size=500, chunk_overlap=50, separators="\n\n")
    chunked_documents = doc_splitter.split()

    if CLEAR_EMBEDDINGS == "True":
        clear_embeddings_collection(DB_COLLECTION)

    store_documents_in_mongodb(chunked_documents)

    context = get_query_results(question, resources_to_retrieve_from)
    print(context)

    
if __name__ == "__main__":
    # Configure Streamable HTTP transport (optionally HTTPS)
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8050"))

    # Start the MCP server using Streamable HTTP transport
    mcp.run(
        transport="http",
        host=host,
        port=port,
    )
    # debug()
