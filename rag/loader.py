"""Load plain-text project notes without depending on the DevCompass app."""

from pathlib import Path
from typing import Iterable

from .models import Document

SUPPORTED_EXTENSIONS = {".md", ".txt"}


def load_documents(paths: Iterable[str | Path]) -> list[Document]:
    """Read Markdown and text files from the supplied files or directories.

    Directories are searched recursively. Files are returned in a stable order
    so repeated runs produce the same document list.
    """
    documents: list[Document] = []

    for item in paths:
        path = Path(item).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Document path does not exist: {path}")

        if path.is_dir():
            candidates = sorted(
                child for child in path.rglob("*")
                if child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        elif path.is_file():
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
                raise ValueError(f"Unsupported document type: {path.suffix or '(none)'}. Use {supported}.")
            candidates = [path]
        else:
            raise ValueError(f"Not a regular file or directory: {path}")

        for candidate in candidates:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                documents.append(Document(source=str(candidate), text=text))

    return documents
