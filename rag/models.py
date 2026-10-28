"""Small data objects shared by the standalone RAG modules."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    """Readable text together with the file it came from."""

    source: str
    text: str


@dataclass(frozen=True)
class Chunk:
    """A short piece of a document, ready to be searched."""

    source: str
    text: str
    index: int


@dataclass(frozen=True)
class SearchResult:
    """A matching chunk and its relative search score."""

    chunk: Chunk
    score: float
