"""Small tests for the standalone RAG example."""

import tempfile
import unittest
from pathlib import Path

from rag.chunker import chunk_document
from rag.loader import load_documents
from rag.models import Document
from rag.pipeline import RAGPipeline
from rag.retriever import search_chunks


class LoaderTests(unittest.TestCase):
    def test_loads_supported_files_from_a_folder_in_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)
            (folder / "b.md").write_text("# Setup\nInstall Python.", encoding="utf-8")
            (folder / "a.txt").write_text("Run the app locally.", encoding="utf-8")
            (folder / "ignore.json").write_text("not loaded", encoding="utf-8")

            documents = load_documents([folder])

        self.assertEqual([Path(item.source).name for item in documents], ["a.txt", "b.md"])
        self.assertEqual(documents[0].text, "Run the app locally.")

    def test_missing_path_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            load_documents(["this-file-should-not-exist.md"])


class ChunkerTests(unittest.TestCase):
    def test_chunks_overlap_and_keep_their_source(self):
        document = Document("notes.md", "one two three four five six")

        chunks = chunk_document(document, chunk_size=4, overlap=1)

        self.assertEqual([chunk.text for chunk in chunks], [
            "one two three four",
            "four five six",
        ])
        self.assertEqual([chunk.index for chunk in chunks], [0, 1])
        self.assertTrue(all(chunk.source == "notes.md" for chunk in chunks))

    def test_invalid_overlap_is_rejected(self):
        with self.assertRaises(ValueError):
            chunk_document(Document("notes.md", "words here"), chunk_size=2, overlap=2)


class RetrievalTests(unittest.TestCase):
    def test_search_ranks_matching_passage_and_omits_unrelated_text(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)
            (folder / "setup.md").write_text(
                "Install Python and start the application from the terminal.",
                encoding="utf-8",
            )
            (folder / "design.txt").write_text(
                "The color palette uses warm paper and muted orange.",
                encoding="utf-8",
            )
            pipeline = RAGPipeline([folder], chunk_size=20, overlap=2)

            results = pipeline.search("how do I install Python", top_k=3)

        self.assertEqual(len(results), 1)
        self.assertEqual(Path(results[0].chunk.source).name, "setup.md")
        self.assertGreater(results[0].score, 0)

    def test_empty_query_returns_no_matches(self):
        document = Document("notes.md", "some useful words")
        chunks = chunk_document(document)

        self.assertEqual(search_chunks("   ", chunks), [])


if __name__ == "__main__":
    unittest.main()
