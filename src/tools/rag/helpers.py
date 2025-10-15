from glob import glob
import hashlib
import json
import time
from operator import itemgetter

from pymongo.operations import SearchIndexModel

from src.logging_setup import configure_logging

logger = configure_logging()


def json_to_str(data) -> str:
    """Stable SHA256 for dicts or lists."""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).strip()
    return blob


def compute_hash(doc):
    """Compute a hash for the document based on its content and source."""
    if type(doc.page_content) == list or str:
        content = doc.page_content.strip() if type(doc.page_content) == str else json_to_str(doc.page_content)
    else:
        raise TypeError("Unknown content type in document. The content should either be string or a list.")
    source = doc.metadata.get("source")
    return hashlib.sha256((source + content).encode("utf-8")).hexdigest()


def clear_embeddings_collection(collection): 
    """Clear the embeddings collection in MongoDB."""
    collection.drop()
    logger.info("Cleared embeddings collection.")


def add_vector_search_index_to_db(collection, embedding_length=1024, similarity_metric="cosine"):
    """Create a vector search index in MongoDB."""
    logger.info("Checking if vector search index already exists in database...")
    index_names = [i["name"] for i in collection.list_search_indexes()]
    if "vector_index" not in index_names:
        logger.info("Creating vector index for embeddings...")
        # Create the index model, then create the search index
        search_index_model = SearchIndexModel(
            definition = {
                "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": embedding_length,
                    "similarity": similarity_metric
                },
                {
                    "type": "filter",
                    "path": "resource_type",
                },
                {
                    "type": "filter",
                    "path": "resource_name",
                },
                ]
            },
            name = "vector_index",
            type = "vectorSearch"
        )
        collection.create_search_index(model=search_index_model)
        # Wait until it is ready
        while collection.list_search_indexes().to_list()[0]["status"] != "READY":
            time.sleep(1)
        logger.info("Vector index created successfully!")
    else:
        logger.info("Vector index already exists.")
    

def is_doc_in_db(doc, db):
    # Hashing on chunk level, not the whole document!
    doc_hash = compute_hash(doc) 
    doc.metadata["file_hash"] = doc_hash
    return db.count_documents({"file_hash": doc_hash}) > 0


def get_new_or_changes_documents(documents, db):
    new_docs = []
    for doc in documents:
        # Check if document already embedded
        if is_doc_in_db(doc, db):
            logger.info(f"Embeddings already exist for {doc.metadata.get('source')}-{doc.metadata.get('chunk_id')}")
            continue  # Skip re-embedding 
        else:
            logger.info(f"Creating embeddings for {doc.metadata.get('source')}-{doc.metadata.get('chunk_id')}")

        new_docs.append(doc)
    return new_docs


def postprocessing_query_result(query_results):
    context = ""
    for result in query_results:
        resource_type = result[0].get("resource_type")
        if resource_type == "document":
            if context: 
                context += "\n\n"
            context += "Here is some context that you can refer to answer the question:\n\n"
            chunks_sorted = sorted(result, key=itemgetter("document", "chunk_id"))
            context += "\n\n".join(document["content"] for document in chunks_sorted)

        elif resource_type == "example":
            if context: 
                context += "\n\n"
            context += "Here are some examples that can help you answer the question:\n\n"\
                       f"### EXAMPLES BEGIN ###\n\n"
            chunks_sorted = sorted(result, key=itemgetter("document", "chunk_id"))
            context += "\n\n".join(document["content"] for document in chunks_sorted)
            context += "\n\n### EXAMPLES END ###"

        else:
            raise (ValueError, f"Unknown resource type: {resource_type}")
    return context