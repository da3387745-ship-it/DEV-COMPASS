"""Find useful passages with a small, dependency-free TF-IDF search."""

import math
import re
from collections import Counter

from .models import Chunk, SearchResult

TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def search_chunks(
    question: str,
    chunks: list[Chunk],
    top_k: int = 5,
) -> list[SearchResult]:
    """Return the most relevant chunks, best match first.

    This lightweight lexical search is useful for examples and small local
    collections. It does not create embeddings or call an AI model.
    """
    if top_k < 1:
        raise ValueError("top_k must be at least 1")

    query_terms = Counter(_tokens(question))
    if not query_terms or not chunks:
        return []

    chunk_terms = [Counter(_tokens(chunk.text)) for chunk in chunks]
    document_frequency = Counter()
    for terms in chunk_terms:
        document_frequency.update(terms.keys())

    total_chunks = len(chunks)

    def weight(term: str, terms: Counter[str]) -> float:
        frequency = terms.get(term, 0)
        if not frequency:
            return 0.0
        inverse_frequency = math.log(
            (total_chunks + 1) / (document_frequency[term] + 1)
        ) + 1
        return (1 + math.log(frequency)) * inverse_frequency

    query_vector = {
        term: weight(term, query_terms)
        for term in query_terms
    }
    query_length = math.sqrt(sum(value * value for value in query_vector.values()))
    if query_length == 0:
        return []

    results: list[SearchResult] = []
    for chunk, terms in zip(chunks, chunk_terms):
        document_vector = {
            term: weight(term, terms)
            for term in query_terms
            if terms.get(term, 0)
        }
        document_length = math.sqrt(
            sum(value * value for value in document_vector.values())
        )
        if document_length == 0:
            continue

        dot_product = sum(
            query_vector[term] * document_vector.get(term, 0.0)
            for term in query_vector
        )
        score = dot_product / (query_length * document_length)
        if score > 0:
            results.append(SearchResult(chunk=chunk, score=score))

    results.sort(key=lambda result: result.score, reverse=True)
    return results[:top_k]
