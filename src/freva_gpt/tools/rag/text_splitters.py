from langchain_text_splitters import RecursiveCharacterTextSplitter


class CustomDocumentSplitter(RecursiveCharacterTextSplitter):
    def __init__(self, documents, separators = None, keep_separator = False, is_separator_regex = False, **kwargs):
        self.documents = documents
        super().__init__(separators, keep_separator, is_separator_regex, **kwargs)

    def split(self):
        splitted_docs = []
        for doc in self.documents:
            embedded_text = doc.metadata["embedded_content"]
            if embedded_text == doc.page_content:
                # Split if it is not an example
                split_text = self.split_text(embedded_text)
                for i, chunk_text in enumerate(split_text):
                    chunk = doc.copy(deep=True)
                    chunk.metadata["chunk_id"] = i+1
                    chunk.metadata["embedded_content"] = chunk_text
                    chunk.page_content = chunk_text
                    splitted_docs.append(chunk)
            else:
                splitted_docs.append(doc)
        return splitted_docs
