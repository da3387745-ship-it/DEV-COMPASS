"""A compact way to load, split, and search a folder of project notes."""

from pathlib import Path
from typing import Iterable

from .chunker import chunk_documents
from .loader import load_documents
from .models import SearchResult
from .retriever import search_chunks


class RAGPipeline:
    """A small in-memory retrieval pipeline for Markdown and text documents."""

    def __init__(
        self,
        paths: Iterable[str | Path],
        chunk_size: int = 120,
        overlap: int = 20,
    ):
        self.documents = load_documents(paths)
        self.chunks = chunk_documents(
            self.documents,
            chunk_size=chunk_size,
            overlap=overlap,
        )

    def search(self, question: str, top_k: int = 5) -> list[SearchResult]:
        """Find passages that best match a question."""
        return search_chunks(question, self.chunks, top_k=top_k)
