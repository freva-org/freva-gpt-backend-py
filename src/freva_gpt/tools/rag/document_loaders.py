import ast
from pathlib import Path
from typing import List

from freva_gpt.logger import logger
from langchain_community.document_loaders import (
    DirectoryLoader,
    JSONLoader,
    PyPDFLoader,
    TextLoader,
)
from langchain_core.documents import Document

loader_cls_dict = {
    ".txt": TextLoader,
    ".json": JSONLoader,
    ".jsonl": JSONLoader,
    ".pdf": PyPDFLoader,
}
loader_kwargs_dict = {
    ".txt": {},
    ".json": {},
    ".jsonl": {"text_content": False, "jq_schema": ".", "json_lines": True},
    ".pdf": {},
}


class CustomDirectoryLoader(DirectoryLoader):
    def __init__(self, path: str, **kwargs):
        super().__init__(path, kwargs)
        self.dir_name = path.split("/")[-1]
        self.extensions = self.list_extensions()

    def list_extensions(self) -> List[str]:
        """Get a list of file extensions in directory"""
        directory = Path(self.path)
        if not directory.exists():
            raise FileNotFoundError(f"Directory not found: {self.path}")
        if not directory.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {self.path}")

        extensions = sorted(
            {file.suffix for file in directory.iterdir() if file.is_file()}
        )
        return extensions

    def load(self) -> List[Document]:
        """Load documents."""
        all_documents = []
        if self.extensions:
            for doc_type in self.extensions:
                if doc_type in loader_cls_dict.keys():
                    self.glob = "*" + doc_type
                    self.loader_cls = loader_cls_dict[doc_type]
                    self.loader_kwargs = loader_kwargs_dict[doc_type]
                    docs = list(self.lazy_load())
                    if doc_type == ".jsonl":
                        docs = self.parse_examples(docs)
                    else:
                        docs = self.standardize_metadata(docs)
                    all_documents.extend(docs)
                else:
                    raise (
                        TypeError,
                        f"The directory contains an unsupported file extension. Please add a document loader mapping for {doc_type} files.",
                    )
        else:
            logger.warning(f"The directory is empty: {self.path}")

        return all_documents

    def parse_examples(self, json_lines: List[Document]) -> List[Document]:
        """
        Parse examples from a JSONL file, grouping a user query and returned answers so that each example is one document.
        Also adds the user query to metadata as "embedded_text".
        """
        examples = []
        current_trace = None

        example_id = 1
        for line in json_lines:
            content = ast.literal_eval(line.page_content)
            if content.get("variant") == "User":
                user_prompt = content.get("content")
                if current_trace:
                    examples.append(current_trace)
                current_trace = line
                current_trace.metadata["chunk_id"] = example_id
                current_trace.metadata["embedded_content"] = (
                    user_prompt  # For examples: no need to embed the whole content, just the user input.
                )
                current_trace.metadata["resource_name"] = self.dir_name
                example_id += 1
                del current_trace.metadata["seq_num"]
            elif current_trace:
                current_trace.page_content += line.page_content

        if current_trace:
            examples.append(current_trace)

        return examples

    def standardize_metadata(self, docs: List[Document]) -> List[Document]:
        """
        Add missing fields ("embedded_content") to metadata for Document object for uniform data fields.
        """
        std_docs = []
        for d in docs:
            d.metadata["chunk_id"] = 1
            d.metadata["embedded_content"] = d.page_content
            d.metadata["resource_name"] = self.dir_name
            std_docs.append(d)
        return std_docs
