"""Document parsing, indexing, retrieval, and grounded answer generation."""

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from groq import Groq
from fastapi import HTTPException
from openai import OpenAI
from pymongo.errors import PyMongoError
from pymongo.operations import ReplaceOne
from pypdf import PdfReader
from pypdf.errors import PdfReadError

load_dotenv()

DEBUG_RETRIEVAL = (
    os.getenv('DEVCOMPASS_DEBUG_RETRIEVAL', '0').lower()
    in {'1', 'true', 'yes', 'on'}
)


def debug_retrieval(message):
    """Print retrieval details when RAG debugging is enabled."""
    if DEBUG_RETRIEVAL:
        print(f"[RAG_DEBUG] {time.strftime('%H:%M:%S')} {message}")


def create_embedding_model(model_name):
    """Load the lightweight ONNX embedding model only when RAG needs it."""
    from fastembed import TextEmbedding

    if model_name == 'all-MiniLM-L6-v2':
        model_name = 'sentence-transformers/all-MiniLM-L6-v2'
    return FastEmbeddingModel(TextEmbedding(
        model_name=model_name,
        cache_dir=os.getenv('FASTEMBED_CACHE_PATH'),
        threads=1,
    ))


class FastEmbeddingModel:
    """Keep the RAG service's encode interface while using CPU ONNX inference."""

    def __init__(self, model):
        self.model = model

    def encode(self, texts):
        embeddings = self.model.embed(texts, batch_size=16)
        return [
            embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
            for embedding in embeddings
        ]


def create_chroma_client(path):
    """Load ChromaDB only when a RAG operation needs its persistent index."""
    import chromadb

    return chromadb.PersistentClient(path=path)


def safe_user_folder(user_id):
    """Map account/workspace IDs to safe, bounded document-folder names."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', str(user_id))[:128] or 'default-user'


class AIProviderNotConfiguredError(RuntimeError):
    """Raised when a document answer needs a model but no provider key exists."""


class RAGService:
    """Read local documents, index their chunks, and answer grounded questions."""

    def __init__(
        self,
        *,
        raw_dir: Path,
        chroma_dir: Path,
        mongo_database,
        embedding_model_factory,
        chroma_client_factory,
        safe_user_folder,
        logger,
        debug_retrieval,
    ):
        self._raw_dir = raw_dir
        self._chroma_dir = chroma_dir
        self._mongo_database = mongo_database
        self._embedding_model_factory = embedding_model_factory
        self._chroma_client_factory = chroma_client_factory
        self._safe_user_folder = safe_user_folder
        self._logger = logger
        self._debug_retrieval = debug_retrieval
        self._embedding_model = None
        self._chroma_client = None
        self._collection = None
        self._resource_lock = threading.Lock()
        self.collection_name = 'devcompass_documents'
        self.vector_collection_name = os.getenv(
            'MONGODB_VECTOR_COLLECTION',
            'document_chunks',
        ).strip() or 'document_chunks'
        self.vector_index_name = os.getenv(
            'MONGODB_VECTOR_INDEX',
            'devcompass_vector',
        ).strip() or 'devcompass_vector'
        self.vector_dimensions = 384
        self.default_user_id = os.getenv('DEVCOMPASS_USER_ID', 'default-user')
        gemini_key = os.getenv('GEMINI_API_KEY')
        groq_key = os.getenv('GROQ_API_KEY')
        openrouter_key = os.getenv('OPENROUTER_API_KEY')
        self.gemini_client = genai.Client(api_key=gemini_key) if gemini_key else None
        self.groq_client = Groq(api_key=groq_key) if groq_key else None
        self.openrouter_client = (
            OpenAI(
                api_key=openrouter_key,
                base_url='https://openrouter.ai/api/v1',
            )
            if openrouter_key
            else None
        )

    @property
    def embedding_model(self):
        if self._embedding_model is not None:
            return self._embedding_model
        with self._resource_lock:
            if self._embedding_model is None:
                self._embedding_model = self._embedding_model_factory(
                    os.getenv('EMBEDDING_MODEL', 'all-MiniLM-L6-v2')
                )
            return self._embedding_model

    @property
    def collection(self):
        if self._collection is not None:
            return self._collection
        with self._resource_lock:
            if self._collection is None:
                self._chroma_client = self._chroma_client_factory(str(self._chroma_dir))
                self._collection = self._chroma_client.get_or_create_collection(
                    name=self.collection_name
                )
            return self._collection

    def document_id_for(self, source_name, user_id=None):
        """Create a repeatable identifier for one source in one workspace."""
        scope_user = user_id or self.default_user_id
        return hashlib.sha1(f"{scope_user}:{source_name}".encode()).hexdigest()

    def effective_user_id(self, user_id=None):
        """Use the requested workspace or this local install's default workspace."""
        return user_id or self.default_user_id

    @property
    def vector_collection(self):
        """Return the Atlas collection containing persisted document chunks."""
        return self._mongo_database()[self.vector_collection_name]

    def active_document_id(self, user_id=None, document_id=None):
        """Choose the requested document, or the most recently changed source."""
        if document_id:
            return document_id
        scope_user = self.effective_user_id(user_id)
        documents = self.load_documents(user_id=scope_user)
        if documents:
            active_document = max(
                documents,
                key=lambda document: Path(document['path']).stat().st_mtime,
            )
            return active_document['document_id']
        if os.getenv('MONGODB_URI', '').strip():
            active = self._mongo_document_metadata(scope_user).sort(
                'updated_at',
                -1,
            ).limit(1)
            document = next(iter(active), None)
            if document:
                return document['_id']
        return None

    def _mongo_document_metadata(self, user_id):
        """Return Atlas file metadata scoped to one workspace."""
        return self._mongo_database()['user_documents'].find(
            {'user_id': user_id},
            {'filename': 1, 'updated_at': 1},
        )

    def read_document(self, path: Path) -> str:
        """Extract readable text from a supported document file."""
        if path.suffix.lower() == '.pdf':
            reader = PdfReader(str(path))
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            return text.strip()
        if path.suffix.lower() == '.ipynb':
            return self.read_notebook(path.read_bytes())
        return path.read_text(encoding='utf-8', errors='ignore')

    def read_uploaded_document(self, filename, contents):
        """Extract text from an upload without relying on ephemeral host storage."""
        suffix = Path(filename).suffix.lower()
        if suffix == '.pdf':
            reader = PdfReader(BytesIO(contents))
            return '\n'.join(page.extract_text() or '' for page in reader.pages).strip()
        if suffix == '.ipynb':
            return self.read_notebook(contents)
        return contents.decode('utf-8', errors='ignore')

    def read_notebook(self, contents):
        """Extract Markdown and source code from a Jupyter notebook, ignoring outputs."""
        try:
            notebook = json.loads(contents.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError('The Jupyter notebook is not valid UTF-8 JSON.') from error
        if not isinstance(notebook, dict) or not isinstance(notebook.get('cells'), list):
            raise ValueError('The Jupyter notebook does not contain a valid cells list.')

        extracted_cells = []
        for index, cell in enumerate(notebook['cells'], start=1):
            if not isinstance(cell, dict) or cell.get('cell_type') not in {'markdown', 'code'}:
                continue
            source = cell.get('source', '')
            if isinstance(source, list):
                source = ''.join(part for part in source if isinstance(part, str))
            if isinstance(source, str) and source.strip():
                label = 'Markdown' if cell['cell_type'] == 'markdown' else 'Code'
                extracted_cells.append(f'[{label} cell {index}]\n{source.strip()}')
        return '\n\n'.join(extracted_cells)

    def load_documents(self, user_id=None):
        """Read supported source files from the local raw-data folder."""
        files = sorted(
            path
            for path in self._raw_dir.rglob('*')
            if path.suffix.lower() in {'.pdf', '.txt', '.md', '.ipynb'}
        )
        if user_id:
            user_directory = self._raw_dir / self._safe_user_folder(user_id)
            files = [path for path in files if user_directory in path.parents]
        documents = []
        active_user = self.effective_user_id(user_id)
        for path in files:
            try:
                text = self.read_document(path)
            except (OSError, PdfReadError, ValueError) as error:
                self._logger.warning(
                    'Skipping unreadable document %s (%s).',
                    path.name,
                    type(error).__name__,
                )
                continue
            if text.strip():
                documents.append(
                    {
                        'source': path.name,
                        'path': str(path),
                        'text': text,
                        'user_id': active_user,
                        'document_id': self.document_id_for(
                            path.name,
                            user_id=active_user,
                        ),
                    }
                )
        return documents

    def chunk_text(self, text, size=500, overlap=50):
        """Split extracted text into overlapping passages for search."""
        clean_text = ' '.join(text.split())
        chunks = []
        start = 0
        while start < len(clean_text):
            end = min(start + size, len(clean_text))
            piece = clean_text[start:end].strip()
            if piece:
                chunks.append(piece)
            if end == len(clean_text):
                break
            start = max(end - overlap, start + 1)
        return chunks

    def rebuild_index(self, user_id=None, documents=None):
        """Index source files in Chroma and Atlas without replacing other files."""
        scope_user = self.effective_user_id(user_id)

        documents = (
            self.load_documents(user_id=scope_user)
            if documents is None
            else documents
        )
        chunks = []
        for document in documents:
            for number, text in enumerate(self.chunk_text(document['text'])):
                chunks.append({
                    'id': hashlib.sha1(
                        f"{document['user_id']}:{document['document_id']}:{number}:{text}".encode()
                    ).hexdigest(),
                    'text': text,
                    'source': document['source'],
                    'document_id': document['document_id'],
                    'user_id': document['user_id'],
                    'chunk': number,
                })

        embeddings = []
        if chunks:
            embeddings = self.embedding_model.encode([item['text'] for item in chunks])
            if hasattr(embeddings, 'tolist'):
                embeddings = embeddings.tolist()

        atlas_collection = None
        if os.getenv('MONGODB_URI', '').strip():
            if any(len(embedding) != self.vector_dimensions for embedding in embeddings):
                raise ValueError(
                    'The configured embedding model must produce '
                    f'{self.vector_dimensions}-dimensional vectors for Atlas Vector Search.'
                )
            atlas_collection = self.vector_collection
            operations = [
                ReplaceOne(
                    {'_id': item['id']},
                    {
                        '_id': item['id'],
                        'embedding': [float(value) for value in embedding],
                        'text': item['text'],
                        'source': item['source'],
                        'document_id': item['document_id'],
                        'user_id': item['user_id'],
                        'chunk': item['chunk'],
                    },
                    upsert=True,
                )
                for item, embedding in zip(chunks, embeddings)
            ]
            for start in range(0, len(operations), 100):
                atlas_collection.bulk_write(
                    operations[start:start + 100],
                    ordered=False,
                )

        existing_ids = self.collection.get(
            where={'user_id': {'$eq': scope_user}}
            if documents is None
            else {
                '$and': [
                    {'user_id': {'$eq': scope_user}},
                    {'document_id': {'$in': [
                        document['document_id'] for document in documents
                    ]}},
                ],
            },
        ).get('ids', [])
        if existing_ids:
            self.collection.delete(ids=existing_ids)

        if documents is None:
            if atlas_collection is not None:
                atlas_collection.delete_many(
                    {
                        'user_id': scope_user,
                        **(
                            {'_id': {'$nin': [item['id'] for item in chunks]}}
                            if chunks
                            else {}
                        ),
                    }
                )
        elif atlas_collection is not None:
            document_ids = [document['document_id'] for document in documents]
            if document_ids:
                atlas_collection.delete_many({
                    'user_id': scope_user,
                    'document_id': {'$in': document_ids},
                    **(
                        {'_id': {'$nin': [item['id'] for item in chunks]}}
                        if chunks
                        else {}
                    ),
                })

        if chunks:
            self.collection.upsert(
                ids=[item['id'] for item in chunks],
                documents=[item['text'] for item in chunks],
                metadatas=[{
                    'source': item['source'],
                    'document_id': item['document_id'],
                    'user_id': item['user_id'],
                    'chunk': item['chunk'],
                } for item in chunks],
                embeddings=embeddings,
            )
        if atlas_collection is not None:
            database = self._mongo_database()
            for document in documents:
                database['user_documents'].update_one(
                    {'_id': document['document_id']},
                    {'$set': {
                        'user_id': scope_user,
                        'filename': document['source'],
                        'chunks': len(self.chunk_text(document['text'])),
                        'updated_at': datetime.now(timezone.utc),
                    }},
                    upsert=True,
                )
        if not chunks:
            return {'documents': len(documents), 'chunks': 0, 'document_id': None}

        active_doc_id = self.active_document_id(user_id=scope_user)
        return {
            'documents': len(documents),
            'chunks': len(chunks),
            'document_id': active_doc_id,
        }

    def retrieve(self, question, limit=5, max_distance=2.0, user_id=None, document_id=None):
        """Find relevant passages with Atlas, retaining Chroma as a fallback."""
        scope_user = self.effective_user_id(user_id)
        scope_filters = [{'user_id': {'$eq': scope_user}}]
        if document_id:
            scope_filters.append({'document_id': {'$eq': document_id}})
        scope = {'$and': scope_filters}

        question_embedding = self.embedding_model.encode([question])
        if hasattr(question_embedding, 'tolist'):
            question_embedding = question_embedding.tolist()

        if os.getenv('MONGODB_URI', '').strip():
            try:
                vector = [float(value) for value in question_embedding[0]]
                if len(vector) != self.vector_dimensions:
                    raise ValueError(
                        'The configured embedding model must produce '
                        f'{self.vector_dimensions}-dimensional vectors for Atlas Vector Search.'
                    )
                filter_query = {'user_id': {'$eq': scope_user}}
                if document_id:
                    filter_query['document_id'] = {'$eq': document_id}
                atlas_result = list(self.vector_collection.aggregate([
                    {'$vectorSearch': {
                        'index': self.vector_index_name,
                        'path': 'embedding',
                        'queryVector': vector,
                        'numCandidates': min(max(limit * 20, 100), 10000),
                        'limit': limit,
                        'filter': filter_query,
                    }},
                    {'$project': {
                        'text': 1,
                        'source': 1,
                        'chunk': 1,
                        'score': {'$meta': 'vectorSearchScore'},
                    }},
                ]))
                matches = []
                for item in atlas_result:
                    distance = 1 - float(item.get('score', 0))
                    if distance <= max_distance:
                        matches.append({
                            'text': item.get('text', ''),
                            'source': item.get('source', 'unknown'),
                            'chunk': item.get('chunk', 0),
                            'distance': round(distance, 4),
                        })
                if matches:
                    return matches
            except (PyMongoError, HTTPException) as error:
                self._logger.warning(
                    'Atlas Vector Search failed; falling back to local Chroma (%s).',
                    type(error).__name__,
                )

        self._debug_retrieval(
            f'collection={self.collection_name} user_id={scope_user} '
            f'document_id={document_id} topK={limit} threshold={max_distance}'
        )

        result = self.collection.query(
            query_embeddings=question_embedding,
            n_results=limit,
            where=scope,
            include=['documents', 'metadatas', 'distances'],
        )
        texts = result.get('documents', [[]])[0]
        metadata = result.get('metadatas', [[]])[0]
        distances = result.get('distances', [[]])[0]

        matches = []
        for text, meta, distance in zip(texts, metadata, distances):
            try:
                value = float(distance)
            except (TypeError, ValueError):
                continue
            if value <= max_distance:
                matches.append({
                    'text': text,
                    'source': meta.get('source', 'unknown'),
                    'chunk': meta.get('chunk', 0),
                    'distance': round(value, 4),
                })

        result_sources = [match['source'] for match in matches]
        self._debug_retrieval(
            f'collection={self.collection_name} '
            f'result_count={len(matches)} sources={result_sources}'
        )
        return matches

    def retrieve_all_documents(self, user_id=None, document_id=None):
        """Return passages from Atlas, falling back to the local Chroma index."""
        scope_user = self.effective_user_id(user_id)
        scope_filters = [{'user_id': {'$eq': scope_user}}]
        if document_id:
            scope_filters.append({'document_id': {'$eq': document_id}})
        scope = {'$and': scope_filters}

        if os.getenv('MONGODB_URI', '').strip():
            try:
                documents = self.vector_collection.find(
                    {
                        'user_id': scope_user,
                        **({'document_id': document_id} if document_id else {}),
                    },
                    {'text': 1, 'source': 1, 'chunk': 1},
                ).sort('chunk', 1).limit(5)
                matches = [
                    {
                        'text': item['text'],
                        'source': item.get('source', 'unknown'),
                        'chunk': item.get('chunk', 0),
                        'distance': 0,
                    }
                    for item in documents
                    if item.get('text')
                ]
                if matches:
                    return matches
            except (PyMongoError, HTTPException) as error:
                self._logger.warning(
                    'Could not read Atlas document chunks; falling back to local Chroma (%s).',
                    type(error).__name__,
                )

        result = self.collection.get(include=['documents', 'metadatas'], where=scope)
        documents = result.get('documents', [])
        metadatas = result.get('metadatas', [])
        matches = []
        for text, meta in zip(documents, metadatas):
            if not text:
                continue
            matches.append({
                'text': text,
                'source': meta.get('source', 'unknown'),
                'chunk': meta.get('chunk', 0),
                'distance': 0,
            })
        return matches

    def keyword_search(self, question, limit=5, user_id=None, document_id=None):
        """Use a simple text match when semantic retrieval finds no passage."""
        scope_user = self.effective_user_id(user_id)
        documents = self.load_documents(user_id=scope_user)
        if document_id:
            documents = [doc for doc in documents if doc['document_id'] == document_id]

        question_words = [
            word.lower()
            for word in re.findall(r'[A-Za-z0-9]+', question)
            if len(word) > 2
        ]
        if not question_words:
            return []

        scored = []
        for document in documents:
            lower_text = document['text'].lower()
            score = sum(1 for word in question_words if word in lower_text)
            if score:
                scored.append((score, document))

        results = []
        ranked_documents = sorted(
            scored,
            key=lambda result: result[0],
            reverse=True,
        )[:3]
        for _, document in ranked_documents:
            for index, chunk in enumerate(self.chunk_text(document['text'])):
                chunk_lower = chunk.lower()
                if any(word in chunk_lower for word in question_words):
                    results.append(
                        {
                            'text': chunk,
                            'source': document['source'],
                            'chunk': index,
                            'distance': 0,
                        }
                    )
                    if len(results) >= limit:
                        return results
        if results or not os.getenv('MONGODB_URI', '').strip():
            return results

        try:
            atlas_chunks = self.vector_collection.find(
                {
                    'user_id': scope_user,
                    **({'document_id': document_id} if document_id else {}),
                },
                {'text': 1, 'source': 1, 'chunk': 1},
            )
            matching_chunks = []
            for item in atlas_chunks:
                text = item.get('text', '')
                score = sum(1 for word in question_words if word in text.lower())
                if score:
                    matching_chunks.append((score, item))
            matching_chunks.sort(
                key=lambda result: result[0],
                reverse=True,
            )
            return [{
                'text': item['text'],
                'source': item.get('source', 'unknown'),
                'chunk': item.get('chunk', 0),
                'distance': 0,
            } for _, item in matching_chunks[:limit]]
        except (PyMongoError, HTTPException) as error:
            self._logger.warning(
                'Atlas keyword fallback failed (%s).',
                type(error).__name__,
            )
        return results

    def build_prompt(self, question, matches, response_style='concise'):
        """Give the model retrieved passages and require a source-grounded answer."""
        context = '\n\n'.join(
            f"Source: {match['source']} (chunk {match['chunk']})\n{match['text']}"
            for match in matches
        )
        length_guidance = (
            'Give a concise, practical answer.'
            if response_style == 'concise'
            else 'Give a thorough, well-structured explanation with useful details.'
        )
        return (
            'You are DevCompass, a careful technical documentation assistant.\n\n'
            'Use only the supplied context to answer this document question. Do not '
            'guess, fill gaps, or use general knowledge to create a confident-sounding '
            "answer. If the context does not clearly support the answer, say: "
            "'I could not find that in the uploaded documents.' "
            f'{length_guidance} Mention the source filename only when the context '
            f'supports the claim.\n\nContext:\n{context}\n\nQuestion: {question}'
        )

    def build_chat_prompt(self, question, response_style='concise'):
        """Prepare a normal chat prompt without document context or citations."""
        length_guidance = (
            'Keep the answer concise and practical.'
            if response_style == 'concise'
            else 'Give a thorough, well-structured explanation with useful details.'
        )
        return (
            'You are DevCompass, a friendly and practical AI assistant for developers.\n'
            'Have a normal conversation and answer naturally. '
            f'{length_guidance} Do not pretend you searched documents and do not '
            f'invent citations.\n\nUser: {question}'
        )

    def ask_gemini(self, prompt):
        """Ask Gemini, or explain that its key has not been configured."""
        if not self.gemini_client:
            raise RuntimeError('GEMINI_API_KEY is not configured')
        response = self.gemini_client.models.generate_content(
            model=os.getenv('GEMINI_MODEL', 'gemini-2.0-flash'),
            contents=prompt,
        )
        return response.text

    def ask_openai_style(self, client, model, prompt):
        """Call a Groq- or OpenRouter-compatible chat API."""
        if not client:
            raise RuntimeError('Provider key is not configured')
        response = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.1,
        )
        return response.choices[0].message.content

    def has_any_provider_key(self):
        """Check whether at least one supported model provider is configured."""
        return any((
            os.getenv('GEMINI_API_KEY'),
            os.getenv('GROQ_API_KEY'),
            os.getenv('OPENROUTER_API_KEY'),
        ))

    def call_providers(self, prompt):
        """Try configured providers in order and report errors if all fail."""
        if not self.has_any_provider_key():
            raise AIProviderNotConfiguredError(
                'No AI provider is configured. Add GEMINI_API_KEY, GROQ_API_KEY, '
                'or OPENROUTER_API_KEY before enabling chat responses.'
            )

        providers = [
            ('gemini', lambda: self.ask_gemini(prompt)),
            (
                'groq',
                lambda: self.ask_openai_style(
                    self.groq_client,
                    os.getenv('GROQ_MODEL', 'llama-3.1-8b-instant'),
                    prompt,
                ),
            ),
            (
                'openrouter',
                lambda: self.ask_openai_style(
                    self.openrouter_client,
                    os.getenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini'),
                    prompt,
                ),
            ),
        ]
        errors = []
        for name, call in providers:
            try:
                return call(), name
            except Exception as error:
                errors.append(f'{name}: {error}')
        raise RuntimeError('All model providers failed. ' + ' | '.join(errors))

    def needs_documents(self, question):
        """Route explicit file and project questions to retrieval."""
        document_terms = {
            'document', 'documents', 'doc', 'docs', 'file', 'files', 'pdf',
            'notebook', 'notebooks', 'jupyter', 'markdown', 'readme', 'source',
            'citation', 'citations', 'repo',
            'repository', 'codebase', 'uploaded', 'attached', 'attachment',
        }
        project_phrases = {
            'this code', 'the code', 'my code', 'this project', 'the project',
            'my project', 'this system', 'the system', 'in this app', 'in this repo',
            'in this repository', 'in this codebase', 'how does the system',
        }
        question_text = f" {' '.join(re.findall(r'[a-z0-9]+', question.lower()))} "
        has_document_reference = any(
            f" {' '.join(re.findall(r'[a-z0-9]+', term))} " in question_text
            for term in document_terms
        )
        return has_document_reference or any(
            f" {phrase} " in question_text for phrase in project_phrases
        )

    def asks_for_uploaded_content(self, question):
        """Recognize broad requests to explain or summarize an uploaded file."""
        question_lower = question.lower()

        file_reference_patterns = (
            'this pdf', 'that pdf', 'given pdf', 'uploaded pdf', 'attached pdf',
            'this notebook', 'that notebook', 'uploaded notebook',
            'attached notebook', 'this jupyter notebook', 'that jupyter notebook',
            'this file', 'that file', 'given file', 'uploaded file', 'attached file',
            'this document', 'that document', 'given document', 'uploaded document',
            'this doc', 'that doc', 'given doc', 'uploaded doc', 'attached doc',
            'the pdf', 'the file', 'the document', 'this report', 'that report',
            'explain the pdf', 'explain this pdf', 'explain that pdf',
            'explain the file', 'explain this file', 'explain that file',
            'explain the document', 'explain this document', 'explain that document',
            'summarize the pdf', 'summarise the pdf', 'summarize this pdf',
            'summarize the file', 'summarize this file', 'summarize that file',
            'what is in the pdf', 'what is in this pdf', 'what is in that pdf',
            'what is in the file', 'what is in this file', 'what is in that file',
            'from pdf', 'from this file', 'from this document', 'from that file',
        )

        if any(term in question_lower for term in file_reference_patterns):
            return True

        question_tokens = re.findall(r'[a-z0-9]+', question_lower)
        file_terms = {'file', 'pdf', 'document', 'doc', 'report', 'notebook', 'jupyter'}
        if 'explain' in question_tokens and any(
            token in file_terms for token in question_tokens
        ):
            return True
        if 'summarize' in question_tokens and any(
            token in file_terms for token in question_tokens
        ):
            return True
        if 'describe' in question_tokens and any(
            token in file_terms for token in question_tokens
        ):
            return True

        return False

    def resolve_ambiguous_file_question(self, question, user_id=None, document_id=None):
        """Choose a named source when possible, otherwise use the active file."""
        scope_user = self.effective_user_id(user_id)
        documents = self.load_documents(user_id=scope_user)
        if not documents:
            if not os.getenv('MONGODB_URI', '').strip():
                return []
            indexed_documents = list(
                self._mongo_document_metadata(scope_user).sort('updated_at', -1)
            )
            if not indexed_documents:
                return []
            selected = next(
                (
                    item for item in indexed_documents
                    if item.get('_id') == document_id
                    or item.get('filename', '').lower() in question.lower()
                ),
                indexed_documents[0],
            )
            return self.retrieve_all_documents(
                user_id=scope_user,
                document_id=document_id or selected['_id'],
            )

        active_document = None
        requested_document = document_id or self.active_document_id(user_id=user_id)
        if requested_document:
            for doc in documents:
                if doc['document_id'] == requested_document:
                    active_document = doc
                    break

        if active_document is None:
            active_document = max(documents, key=lambda item: Path(item['path']).stat().st_mtime)

        question_lower = question.lower()
        explicit_candidates = []
        for doc in documents:
            source_name = Path(doc['source']).name.lower()
            stem = Path(doc['source']).stem.lower()
            normalized_stem = re.sub(r'[^a-z0-9]+', ' ', stem).strip()
            candidate_names = {
                source_name,
                stem,
                normalized_stem,
                normalized_stem.replace(' ', '_'),
                normalized_stem.replace(' ', '-'),
            }
            if any(name in question_lower for name in candidate_names):
                explicit_candidates.append(doc)

        if explicit_candidates:
            exact_name_match = [
                document
                for document in explicit_candidates
                if Path(document['source']).name.lower() in question_lower
            ]
            chosen_document = exact_name_match[0] if exact_name_match else explicit_candidates[0]
            return [
                {
                    'text': chunk,
                    'source': chosen_document['source'],
                    'chunk': index,
                    'distance': 0,
                }
                for index, chunk in enumerate(
                    self.chunk_text(chosen_document['text'])[:5]
                )
            ]

        related_terms = {
            'python', 'practice', 'roadmap', 'syllabus',
            'journal', 'dev_compass', 'ai', 'csi',
        }
        for doc in documents:
            source = Path(doc['source']).name.lower()
            if any(term in source for term in related_terms) and any(
                term in question_lower for term in related_terms
            ):
                return [
                    {
                        'text': chunk,
                        'source': doc['source'],
                        'chunk': index,
                        'distance': 0,
                    }
                    for index, chunk in enumerate(
                        self.chunk_text(doc['text'])[:5]
                    )
                ]

        return [
            {
                'text': chunk,
                'source': active_document['source'],
                'chunk': index,
                'distance': 0,
            }
            for index, chunk in enumerate(
                self.chunk_text(active_document['text'])[:5]
            )
        ]

    def ask(self, question, user_id=None, document_id=None, response_style='concise'):
        """Route greetings, general chat, and document questions to their paths."""
        question = question.strip()
        if not question:
            raise ValueError('Question cannot be empty')

        greeting_words = {
            'hi', 'hii', 'hiii', 'hy', 'hyy', 'hyyy', 'hey', 'heyy', 'heyyy',
            'hello', 'helloo', 'hellooo', 'hiya', 'good morning', 'good afternoon',
            'good evening', 'how are you', 'how are you doing',
        }
        normalized_question = question.lower().rstrip('!.?').strip()
        if normalized_question in greeting_words:
            return {
                'answer': (
                    'Hi! I am DevCompass. I am doing well and ready to help '
                    'with your documents.'
                ),
                'sources': [],
                'provider': 'local',
                'mode': 'chat',
            }

        if not self.needs_documents(question):
            if not self.has_any_provider_key():
                return {
                    'answer': (
                        'AI answers need a provider key. Add GEMINI_API_KEY, '
                        'GROQ_API_KEY, or OPENROUTER_API_KEY to the backend .env '
                        'file and restart DevCompass.'
                    ),
                    'sources': [],
                    'provider': 'local',
                    'mode': 'chat',
                }
            answer, provider = self.call_providers(self.build_chat_prompt(question, response_style))
            return {'answer': answer, 'sources': [], 'provider': provider, 'mode': 'chat'}

        scope_user = self.effective_user_id(user_id)
        scope_document = document_id or self.active_document_id(user_id=scope_user)

        if self.asks_for_uploaded_content(question):
            matches = self.resolve_ambiguous_file_question(
                question,
                user_id=scope_user,
                document_id=scope_document,
            )
            if not matches:
                matches = self.retrieve_all_documents(user_id=scope_user, document_id=scope_document)
        else:
            matches = self.retrieve(question, user_id=scope_user, document_id=scope_document)

        if not matches:
            matches = self.keyword_search(question, user_id=scope_user, document_id=scope_document)

        if not matches:
            return {
                'answer': 'I could not find that in the uploaded documents.',
                'sources': [],
                'provider': 'local',
                'mode': 'rag',
            }

        answer, provider = self.call_providers(self.build_prompt(question, matches, response_style))
        return {
            'answer': answer,
            'sources': sorted({item['source'] for item in matches}),
            'provider': provider,
            'mode': 'rag',
        }
