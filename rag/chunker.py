"""Split longer documents into overlapping, readable passages."""

from .models import Chunk, Document


def chunk_document(
    document: Document,
    chunk_size: int = 120,
    overlap: int = 20,
) -> list[Chunk]:
    """Split a document by words, keeping a little context between passages."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be between 0 and chunk_size - 1")

    words = document.text.split()
    if not words:
        return []

    chunks: list[Chunk] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(Chunk(
            source=document.source,
            text=" ".join(words[start:end]),
            index=len(chunks),
        ))
        if end == len(words):
            break
        start = end - overlap

    return chunks


def chunk_documents(
    documents: list[Document],
    chunk_size: int = 120,
    overlap: int = 20,
) -> list[Chunk]:
    """Chunk several documents while keeping each file's chunk numbering."""
    return [
        chunk
        for document in documents
        for chunk in chunk_document(document, chunk_size, overlap)
    ]
