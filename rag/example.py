"""Try the standalone retriever: python -m rag.example ./your-notes"""

import argparse

from .pipeline import RAGPipeline


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search Markdown and text files in a folder."
    )
    parser.add_argument("folder", help="Folder containing .md or .txt files")
    parser.add_argument("question", help="Words or question to search for")
    args = parser.parse_args()

    pipeline = RAGPipeline([args.folder])
    results = pipeline.search(args.question)
    if not results:
        print("No matching passages found.")
        return

    for result in results:
        print(f"\n{result.chunk.source} · score {result.score:.3f}")
        print(result.chunk.text)


if __name__ == "__main__":
    main()
