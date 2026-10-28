# Standalone RAG example

This folder is an independent, small retrieval example. It does not import the
DevCompass backend, use its database, or change the running application.

It uses only Python's standard library and handles `.txt` and `.md` files:

1. `loader.py` reads files from paths or folders.
2. `chunker.py` makes overlapping word-based passages.
3. `retriever.py` ranks passages with simple TF-IDF cosine similarity.
4. `pipeline.py` brings those steps together in memory.

This example retrieves text; it does not generate an AI answer or produce
vector embeddings.

From the repository root, run:

```powershell
python -m rag.example .\some-notes "how does login work"
```

Or use the pipeline in another standalone script:

```python
from rag import RAGPipeline

notes = RAGPipeline(["some-notes"])
for result in notes.search("how does login work", top_k=3):
    print(result.chunk.source, result.chunk.text)
```
