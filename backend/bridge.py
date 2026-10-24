"""Local API for DevCompass document uploads, search, and chat."""

import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import time

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google import genai
from groq import Groq
from openai import OpenAI
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.operations import ReplaceOne
from pypdf import PdfReader
from pypdf.errors import PdfReadError

load_dotenv()

logger = logging.getLogger(__name__)
DEBUG_RETRIEVAL = (
    os.getenv('DEVCOMPASS_DEBUG_RETRIEVAL', '0').lower()
    in {'1', 'true', 'yes', 'on'}
)


def debug_retrieval(message):
    """Print retrieval details when RAG debugging is enabled."""
    if DEBUG_RETRIEVAL:
        print(f"[RAG_DEBUG] {time.strftime('%H:%M:%S')} {message}")


def create_embedding_model(model_name):
    """Load the embedding dependency only when a RAG operation needs it."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


def create_chroma_client(path):
    """Load ChromaDB only when a RAG operation needs its persistent index."""
    import chromadb

    return chromadb.PersistentClient(path=path)


PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_DIR.parent / 'frontend' / 'dist'
RAW_DIR = Path(os.getenv('DEVCOMPASS_RAW_DIR', PROJECT_DIR / 'data' / 'raw'))
CHROMA_DIR = Path(os.getenv('DEVCOMPASS_CHROMA_DIR', PROJECT_DIR / 'storage' / 'chroma'))
RAW_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title='DevCompass RAG Bridge')
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        'http://localhost:5173',
        'http://127.0.0.1:5173',
        'http://localhost:8001',
        'http://127.0.0.1:8001',
        'null',
        'https://dev-compass-1.onrender.com',
    ],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

_mongo_client = None
SESSION_COOKIE = 'devcompass_session'
GUEST_COOKIE = 'devcompass_guest'
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 7
PASSWORD_HASH_ITERATIONS = 310_000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def secure_cookies_enabled():
    """Use secure cookies by default on Render, while allowing local overrides."""
    configured_value = os.getenv('DEVCOMPASS_COOKIE_SECURE')
    if configured_value is None:
        return os.getenv('RENDER', '').lower() == 'true'
    return configured_value.lower() in {'1', 'true', 'yes', 'on'}


def session_cookie_samesite():
    """Allow cross-origin HTTPS frontend requests in production."""
    return 'none' if secure_cookies_enabled() else 'lax'


def get_mongo_client():
    """Create and reuse the backend MongoDB client for the process lifetime."""
    global _mongo_client
    uri = os.getenv('MONGODB_URI', '').strip()
    if not uri:
        raise HTTPException(
            status_code=503,
            detail=(
                'MongoDB is not configured. Add MONGODB_URI to the project '
                '.env file and restart DevCompass.'
            ),
        )
    if _mongo_client is None:
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000, appname='DevCompass')
    return _mongo_client


def close_mongo_client():
    """Close the shared client when the backend shuts down."""
    global _mongo_client
    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None


def mongo_database():
    """Return the configured DevCompass database."""
    database_name = os.getenv('MONGODB_DATABASE', 'devcompass').strip() or 'devcompass'
    return get_mongo_client()[database_name]


def ensure_auth_indexes():
    """Create indexes needed for unique accounts and expiring sessions."""
    database = mongo_database()
    database['users'].create_index('email', unique=True)
    database['sessions'].create_index('expires_at', expireAfterSeconds=0)


def hash_password(password):
    """Store a salted PBKDF2 password digest."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return f'pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    """Check a password against its encoded PBKDF2 digest."""
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split('$', 3)
        if algorithm != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def public_user(user):
    """Return only the account fields safe to send to the frontend."""
    return {
        'email': user['email'],
        'username': user.get('display_name', ''),
        'has_avatar': bool(user.get('avatar')),
        'settings': user.get('settings', {'theme': 'light', 'answer_style': 'concise'}),
    }


def create_login_session(user, response):
    """Save a hashed session token and issue its secure browser cookie."""
    database = mongo_database()
    token = secrets.token_urlsafe(32)
    database['sessions'].insert_one({
        '_id': hashlib.sha256(token.encode('utf-8')).hexdigest(),
        'user_id': user['_id'],
        'created_at': datetime.now(timezone.utc),
        'expires_at': datetime.now(timezone.utc) + timedelta(seconds=SESSION_LIFETIME_SECONDS),
    })
    secure_cookie = secure_cookies_enabled()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_LIFETIME_SECONDS,
        httponly=True,
        secure=secure_cookie,
        samesite=session_cookie_samesite(),
        path='/',
    )


def request_user(request):
    """Resolve the current user from the browser's session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        database = mongo_database()
        session = database['sessions'].find_one(
            {
                '_id': hashlib.sha256(token.encode('utf-8')).hexdigest(),
                'expires_at': {'$gt': datetime.now(timezone.utc)},
            }
        )
        if not session:
            return None
        return database['users'].find_one({'_id': session['user_id']})
    except PyMongoError as error:
        raise HTTPException(status_code=503, detail='Could not verify the account session.') from error


def require_user(request):
    """Return the signed-in user or raise an authentication error."""
    user = request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail='Please log in to access this account data.')
    return user


def guest_workspace_id(request):
    token = request.cookies.get(GUEST_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail='Refresh the guest session and try again.')
    return f"guest-{hashlib.sha256(token.encode('utf-8')).hexdigest()[:32]}"


def record_activity(user_id, activity_type, title, detail):
    """Save one activity item for the given account."""
    mongo_database()['activity'].insert_one({
        'user_id': user_id,
        'type': activity_type,
        'title': title,
        'detail': detail,
        'created_at': datetime.now(timezone.utc),
    })


def save_chat_turn(question, user_id, response, chat_id=None):
    """Persist a response as a turn in its parent chat."""
    try:
        database = mongo_database()
        turn_index = (
            database['chat_history'].count_documents({'user_id': user_id, 'chat_id': chat_id})
            if chat_id
            else None
        )
        turn = {
            '_id': secrets.token_hex(16),
            'user_id': user_id,
            'question': question,
            'answer': response['answer'],
            'sources': response.get('sources', []),
            'provider': response.get('provider'),
            'mode': response.get('mode'),
            'created_at': datetime.now(timezone.utc),
        }
        if chat_id:
            turn['chat_id'] = chat_id
            turn['turn_index'] = turn_index
        if response.get('document_id'):
            turn['document_id'] = response['document_id']
        result = database['chat_history'].insert_one(turn)
        if chat_id:
            update = {'$set': {'updated_at': datetime.now(timezone.utc)}}
            if turn_index == 0:
                update['$set']['title'] = question[:80] or 'New chat'
            database['chat_sessions'].update_one(
                {'_id': chat_id, 'user_id': user_id},
                update,
            )
        record_activity(user_id, 'question', question[:120], 'Chat question answered')
        return str(result.inserted_id)
    except HTTPException:
        raise
    except (PyMongoError, ValueError) as error:
        raise HTTPException(
            status_code=503,
            detail=(
                'Could not save this chat to MongoDB. Check the Atlas URI, '
                'network access, and database user.'
            ),
        ) from error


@app.on_event('shutdown')
def shutdown_mongodb():
    close_mongo_client()


class AskRequest(BaseModel):
    """Question and optional local workspace/document scope from the chat UI."""

    question: str
    document_id: str | None = None
    chat_id: str | None = None


class ChatSessionRequest(BaseModel):
    title: str | None = None


class EditTurnRequest(BaseModel):
    question: str


class SignupRequest(BaseModel):
    email: str
    password: str
    display_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class ProfileUpdateRequest(BaseModel):
    display_name: str
    theme: str
    answer_style: str


class PasswordConfirmRequest(BaseModel):
    password: str


def clean_filename(filename):
    """Keep uploaded filenames inside the project's document folder."""
    name = Path(filename or 'document').name
    return re.sub(r'[^A-Za-z0-9._-]', '_', name)


def safe_user_folder(user_id):
    """Map account/workspace IDs to safe, bounded document-folder names."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', str(user_id))[:128] or 'default-user'


class AIProviderNotConfiguredError(RuntimeError):
    """Raised when a document answer needs a model but no provider key exists."""


class RAGService:
    """Read local documents, index their chunks, and answer grounded questions."""

    def __init__(self):
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
                self._embedding_model = create_embedding_model(
                    os.getenv('EMBEDDING_MODEL', 'all-MiniLM-L6-v2')
                )
            return self._embedding_model

    @property
    def collection(self):
        if self._collection is not None:
            return self._collection
        with self._resource_lock:
            if self._collection is None:
                self._chroma_client = create_chroma_client(str(CHROMA_DIR))
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
        return mongo_database()[self.vector_collection_name]

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
        return mongo_database()['user_documents'].find(
            {'user_id': user_id},
            {'filename': 1, 'updated_at': 1},
        )

    def read_document(self, path: Path) -> str:
        """Extract readable text from a PDF, text, or Markdown file."""
        if path.suffix.lower() == '.pdf':
            reader = PdfReader(str(path))
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            return text.strip()
        return path.read_text(encoding='utf-8', errors='ignore')

    def read_uploaded_document(self, filename, contents):
        """Extract text from an upload without relying on ephemeral host storage."""
        if Path(filename).suffix.lower() == '.pdf':
            reader = PdfReader(BytesIO(contents))
            return '\n'.join(page.extract_text() or '' for page in reader.pages).strip()
        return contents.decode('utf-8', errors='ignore')

    def load_documents(self, user_id=None):
        """Read supported source files from the local raw-data folder."""
        files = sorted(
            path
            for path in RAW_DIR.rglob('*')
            if path.suffix.lower() in {'.pdf', '.txt', '.md'}
        )
        if user_id:
            user_directory = RAW_DIR / safe_user_folder(user_id)
            files = [path for path in files if user_directory in path.parents]
        documents = []
        active_user = self.effective_user_id(user_id)
        for path in files:
            try:
                text = self.read_document(path)
            except (OSError, PdfReadError, ValueError) as error:
                logger.warning(
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
            database = mongo_database()
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
                logger.warning(
                    'Atlas Vector Search failed; falling back to local Chroma (%s).',
                    type(error).__name__,
                )

        debug_retrieval(
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
        debug_retrieval(
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
                logger.warning(
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
            logger.warning(
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
            'markdown', 'readme', 'source', 'citation', 'citations', 'repo',
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
        file_terms = {'file', 'pdf', 'document', 'doc', 'report'}
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


rag_service = RAGService()


@app.post('/auth/signup')
def signup(payload: SignupRequest, response: Response):
    email = payload.email.strip().lower()
    display_name = payload.display_name.strip()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        raise HTTPException(status_code=400, detail='Enter a valid email address.')
    if not display_name or len(display_name) > 80:
        raise HTTPException(status_code=400, detail='Display name must be between 1 and 80 characters.')
    if len(payload.password) < 12 or len(payload.password) > 256:
        raise HTTPException(status_code=400, detail='Password must be between 12 and 256 characters.')

    try:
        ensure_auth_indexes()
        user = {
            '_id': secrets.token_hex(16),
            'email': email,
            'password_hash': hash_password(payload.password),
            'display_name': display_name,
            'settings': {'theme': 'light', 'answer_style': 'concise'},
            'created_at': datetime.now(timezone.utc),
        }
        mongo_database()['users'].insert_one(user)
        create_login_session(user, response)
        return {'user': public_user(user)}
    except DuplicateKeyError as error:
        raise HTTPException(
            status_code=409,
            detail='An account with this email already exists.',
        ) from error
    except PyMongoError as error:
        raise HTTPException(
            status_code=503,
            detail='Could not create the account in MongoDB.',
        ) from error


@app.post('/auth/login')
def login(payload: LoginRequest, response: Response):
    email = payload.email.strip().lower()
    if len(payload.password) > 256:
        raise HTTPException(status_code=401, detail='Email or password is incorrect.')
    try:
        ensure_auth_indexes()
        user = mongo_database()['users'].find_one({'email': email})
        if not user or not verify_password(payload.password, user.get('password_hash', '')):
            raise HTTPException(status_code=401, detail='Email or password is incorrect.')
        create_login_session(user, response)
        return {'user': public_user(user)}
    except HTTPException:
        raise
    except PyMongoError as error:
        raise HTTPException(status_code=503, detail='Could not log in using MongoDB.') from error


@app.get('/auth/me')
def auth_me(request: Request):
    user = request_user(request)
    return {'authenticated': user is not None, 'user': public_user(user) if user else None}


@app.post('/auth/logout')
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            mongo_database()['sessions'].delete_one({
                '_id': hashlib.sha256(token.encode('utf-8')).hexdigest(),
            })
        except PyMongoError as error:
            raise HTTPException(status_code=503, detail='Could not end the account session.') from error
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=secure_cookies_enabled(),
        samesite=session_cookie_samesite(),
        path='/',
    )
    return {'ok': True}


@app.get('/guest/session')
def create_guest_session(request: Request, response: Response):
    if not request.cookies.get(GUEST_COOKIE):
        response.set_cookie(
            GUEST_COOKIE,
            secrets.token_urlsafe(32),
            max_age=SESSION_LIFETIME_SECONDS,
            httponly=True,
            secure=secure_cookies_enabled(),
            samesite=session_cookie_samesite(),
            path='/',
        )
    return {'ok': True}


@app.get('/profile')
def get_profile(request: Request):
    return public_user(require_user(request))


@app.put('/profile')
def update_profile(payload: ProfileUpdateRequest, request: Request):
    user = require_user(request)
    display_name = payload.display_name.strip()
    if not display_name or len(display_name) > 80:
        raise HTTPException(status_code=400, detail='Display name must be between 1 and 80 characters.')
    if payload.theme not in {'light', 'dark'}:
        raise HTTPException(status_code=400, detail='Theme must be light or dark.')
    if payload.answer_style not in {'concise', 'detailed'}:
        raise HTTPException(status_code=400, detail='Answer style must be concise or detailed.')
    settings = {'theme': payload.theme, 'answer_style': payload.answer_style}
    try:
        mongo_database()['users'].update_one(
            {'_id': user['_id']},
            {'$set': {'display_name': display_name, 'settings': settings}},
        )
    except PyMongoError as error:
        raise HTTPException(
            status_code=503,
            detail='Could not save your profile and settings.',
        ) from error
    user['display_name'] = display_name
    user['settings'] = settings
    return public_user(user)


@app.post('/profile/avatar')
async def update_profile_avatar(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    allowed_types = {'image/jpeg', 'image/png', 'image/webp'}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail='Choose a PNG, JPEG, or WebP image.')
    image = await file.read(2_000_001)
    if not image or len(image) > 2_000_000:
        raise HTTPException(status_code=400, detail='Profile images must be smaller than 2 MB.')
    try:
        mongo_database()['users'].update_one(
            {'_id': user['_id']},
            {'$set': {'avatar': image, 'avatar_type': file.content_type}},
        )
    except PyMongoError as error:
        raise HTTPException(status_code=503, detail='Could not save your profile image.') from error
    return {'ok': True}


@app.get('/profile/avatar')
def get_profile_avatar(request: Request):
    user = require_user(request)
    image = user.get('avatar')
    if not image:
        raise HTTPException(status_code=404, detail='No profile image has been set.')
    return Response(
        content=image,
        media_type=user.get('avatar_type', 'image/jpeg'),
        headers={'Cache-Control': 'private, max-age=300'},
    )


@app.delete('/profile')
def delete_account(payload: PasswordConfirmRequest, request: Request, response: Response):
    user = require_user(request)
    if not verify_password(payload.password, user.get('password_hash', '')):
        raise HTTPException(status_code=401, detail='Password confirmation did not match.')
    user_id = user['_id']
    database = mongo_database()
    try:
        database['sessions'].delete_many({'user_id': user_id})
        for collection in (
            'chat_sessions',
            'chat_history',
            'activity',
            'user_documents',
            rag_service.vector_collection_name,
        ):
            database[collection].delete_many({'user_id': user_id})
        database['users'].delete_one({'_id': user_id})
        user_directory = RAW_DIR / safe_user_folder(user_id)
        if user_directory.exists() and user_directory.parent == RAW_DIR:
            shutil.rmtree(user_directory)
        collection = rag_service.collection
        indexed_ids = collection.get(where={'user_id': {'$eq': user_id}}).get('ids', [])
        if indexed_ids:
            collection.delete(ids=indexed_ids)
    except (OSError, PyMongoError, ValueError) as error:
        raise HTTPException(status_code=503, detail='Could not completely delete this account.') from error
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=secure_cookies_enabled(),
        samesite=session_cookie_samesite(),
        path='/',
    )
    return {'ok': True}


@app.get('/dashboard')
def dashboard(request: Request):
    user = require_user(request)
    try:
        database = mongo_database()
        user_id = user['_id']
        document_totals = list(database['user_documents'].aggregate([
            {'$match': {'user_id': user_id}},
            {'$group': {'_id': None, 'count': {'$sum': 1}}},
        ]))
        totals = document_totals[0] if document_totals else {'count': 0}
        activities = list(database['activity'].find(
            {'user_id': user_id},
            {'_id': 0, 'type': 1, 'title': 1, 'detail': 1, 'created_at': 1},
        ).sort('created_at', -1).limit(10))
        return {
            'question_count': database['chat_history'].count_documents({'user_id': user_id}),
            'document_count': totals['count'],
            'activity': activities,
        }
    except PyMongoError as error:
        raise HTTPException(status_code=503, detail='Could not load your dashboard data.') from error


@app.get('/chat/history')
def chat_history(request: Request, limit: int = 20):
    user = require_user(request)
    limit = min(max(limit, 1), 50)
    try:
        database = mongo_database()
        legacy_turns = database['chat_history'].find(
            {'user_id': user['_id'], 'chat_id': {'$exists': False}},
            {'question': 1, 'created_at': 1},
        )
        for turn in legacy_turns:
            chat_id = secrets.token_hex(16)
            created_at = turn.get('created_at', datetime.now(timezone.utc))
            database['chat_sessions'].insert_one({
                '_id': chat_id,
                'user_id': user['_id'],
                'title': (turn.get('question') or 'Saved chat')[:80],
                'created_at': created_at,
                'updated_at': created_at,
            })
            database['chat_history'].update_one(
                {'_id': turn['_id'], 'user_id': user['_id']},
                {'$set': {'chat_id': chat_id}},
            )
        sessions = list(database['chat_sessions'].find(
            {'user_id': user['_id']},
            {'_id': 1, 'title': 1, 'created_at': 1, 'updated_at': 1},
        ).sort('updated_at', -1).limit(limit))
        return {'sessions': sessions}
    except PyMongoError as error:
        raise HTTPException(status_code=503, detail='Could not load your saved chat history.') from error


@app.post('/chat/session')
def create_chat_session(request: Request, payload: ChatSessionRequest | None = None):
    user = require_user(request)
    title = (payload.title if payload else '') or 'New chat'
    now = datetime.now(timezone.utc)
    session = {
        '_id': secrets.token_hex(16),
        'user_id': user['_id'],
        'title': title.strip()[:80] or 'New chat',
        'created_at': now,
        'updated_at': now,
    }
    try:
        mongo_database()['chat_sessions'].insert_one(session)
        return {
            'id': session['_id'],
            'title': session['title'],
            'created_at': now,
            'updated_at': now,
        }
    except PyMongoError as error:
        raise HTTPException(status_code=503, detail='Could not create a chat session.') from error


@app.get('/chat/session/{chat_id}')
def get_chat_session(chat_id: str, request: Request):
    user = require_user(request)
    database = mongo_database()
    session = database['chat_sessions'].find_one(
        {'_id': chat_id, 'user_id': user['_id']},
        {'_id': 1, 'title': 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail='Chat session not found.')
    turns = list(database['chat_history'].find(
        {'user_id': user['_id'], 'chat_id': chat_id},
        {'question': 1, 'answer': 1, 'sources': 1, 'created_at': 1},
    ).sort([('turn_index', 1), ('created_at', 1)]))
    for turn in turns:
        turn['id'] = str(turn.pop('_id'))
    return {'id': session['_id'], 'title': session.get('title', 'New chat'), 'turns': turns}


@app.put('/chat/session/{chat_id}/turn/{turn_id}')
def edit_chat_turn(
    chat_id: str,
    turn_id: str,
    payload: EditTurnRequest,
    request: Request,
):
    user = require_user(request)
    question = payload.question.strip()
    if not question or len(question) > 10_000:
        raise HTTPException(status_code=400, detail='Enter a prompt up to 10,000 characters.')
    database = mongo_database()
    turn = database['chat_history'].find_one({
        '_id': turn_id,
        'user_id': user['_id'],
        'chat_id': chat_id,
    })
    if not turn:
        raise HTTPException(status_code=404, detail='This prompt could not be found.')
    try:
        result = rag_service.ask(
            question,
            user_id=user['_id'],
            document_id=turn.get('document_id'),
            response_style=user.get('settings', {}).get('answer_style', 'concise'),
        )
        database['chat_history'].update_one(
            {'_id': turn_id, 'user_id': user['_id'], 'chat_id': chat_id},
            {'$set': {
                'question': question,
                'answer': result['answer'],
                'sources': result.get('sources', []),
                'provider': result.get('provider'),
                'mode': result.get('mode'),
                'updated_at': datetime.now(timezone.utc),
            }},
        )
        database['chat_sessions'].update_one(
            {'_id': chat_id, 'user_id': user['_id']},
            {'$set': {'updated_at': datetime.now(timezone.utc)}},
        )
        result.pop('provider', None)
        result.pop('mode', None)
        result.pop('document_id', None)
        return result
    except HTTPException:
        raise
    except Exception as error:
        logger.exception('Could not regenerate edited chat turn.')
        raise HTTPException(status_code=503, detail='Could not update this answer.') from error


@app.patch('/chat/session/{chat_id}')
def rename_chat_session(chat_id: str, payload: ChatSessionRequest, request: Request):
    user = require_user(request)
    title = (payload.title or '').strip()
    if not title or len(title) > 80:
        raise HTTPException(status_code=400, detail='Chat name must be between 1 and 80 characters.')
    result = mongo_database()['chat_sessions'].update_one(
        {'_id': chat_id, 'user_id': user['_id']},
        {'$set': {'title': title, 'updated_at': datetime.now(timezone.utc)}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail='Chat session not found.')
    return {'id': chat_id, 'title': title}


@app.delete('/chat/session/{chat_id}')
def delete_chat_session(chat_id: str, request: Request):
    user = require_user(request)
    database = mongo_database()
    result = database['chat_sessions'].delete_one({'_id': chat_id, 'user_id': user['_id']})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail='Chat session not found.')
    database['chat_history'].delete_many({'user_id': user['_id'], 'chat_id': chat_id})
    return {'ok': True}


@app.get('/documents')
def list_documents(request: Request):
    """List the current user's source files for the chat library."""
    user = request_user(request)
    user_id = user['_id'] if user else guest_workspace_id(request)
    documents = {
        item['document_id']: {
            'filename': item['source'],
            'document_id': item['document_id'],
        }
        for item in rag_service.load_documents(user_id=user_id)
    }
    if os.getenv('MONGODB_URI', '').strip():
        for item in rag_service._mongo_document_metadata(user_id).sort(
            'updated_at',
            -1,
        ):
            documents.setdefault(item['_id'], {
                'filename': item.get('filename', 'Document'),
                'document_id': item['_id'],
            })
    active_document_id = rag_service.active_document_id(user_id=user_id)
    return {
        'documents': [{
            **item,
            'active': item['document_id'] == active_document_id,
        } for item in documents.values()],
    }


@app.get('/status')
def status():
    """Report vector search readiness without re-embedding files on a GET."""
    if not os.getenv('MONGODB_URI', '').strip():
        return {
            'ok': True,
            'vector_search': 'not_configured',
            'message': 'Using the local Chroma document index.',
        }
    try:
        indexes = list(rag_service.vector_collection.list_search_indexes())
    except PyMongoError as error:
        logger.warning('Could not check Atlas Vector Search readiness (%s).', type(error).__name__)
        return {
            'ok': False,
            'vector_search': 'unavailable',
            'message': 'Atlas Vector Search is temporarily unavailable.',
        }
    index = next(
        (
            item for item in indexes
            if item.get('name') == rag_service.vector_index_name
            and item.get('type') == 'vectorSearch'
        ),
        None,
    )
    ready = bool(index and index.get('status') == 'READY' and index.get('queryable', True))
    return {
        'ok': ready,
        'vector_search': 'ready' if ready else 'index_not_ready',
        'message': (
            'Atlas Vector Search is ready.'
            if ready
            else 'The Atlas Vector Search index is not ready.'
        ),
    }


@app.get('/database/status')
def database_status(request: Request):
    """Ping MongoDB Atlas using backend-only environment configuration."""
    require_user(request)
    uri = os.getenv('MONGODB_URI', '').strip()
    if not uri:
        return {
            'ok': False,
            'configured': False,
            'message': 'Set MONGODB_URI in the backend environment to connect Atlas.',
        }

    client = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000, appname='DevCompass')
        client.admin.command('ping')
        return {'ok': True, 'configured': True}
    except (PyMongoError, ValueError) as error:
        raise HTTPException(
            status_code=503,
            detail='MongoDB is temporarily unavailable.',
        ) from error
    finally:
        if client is not None:
            client.close()


@app.post('/ask')
def ask(payload: AskRequest, request: Request):
    """Answer one chat question using general chat or document retrieval."""
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail='Question cannot be empty')
    user = request_user(request)
    if payload.chat_id and not user:
        raise HTTPException(
            status_code=401,
            detail=(
                'Sign in is only needed to continue a saved chat. Start a new chat '
                'to keep using DevCompass as a guest.'
            ),
        )
    if payload.chat_id:
        session = mongo_database()['chat_sessions'].find_one(
            {'_id': payload.chat_id, 'user_id': user['_id']},
            {'_id': 1},
        )
        if not session:
            raise HTTPException(status_code=404, detail='Chat session not found.')
    user_id = user['_id'] if user else guest_workspace_id(request)
    response_style = user.get('settings', {}).get('answer_style', 'concise') if user else 'concise'
    try:
        result = rag_service.ask(
            payload.question,
            user_id=user_id,
            document_id=payload.document_id,
            response_style=response_style,
        )
    except AIProviderNotConfiguredError as error:
        raise HTTPException(
            status_code=503,
            detail=(
                'AI answers need a provider key. Add GEMINI_API_KEY, GROQ_API_KEY, '
                'or OPENROUTER_API_KEY to the backend .env file and restart DevCompass.'
            ),
        ) from error
    except Exception as error:
        logger.exception('Could not complete a chat request.')
        raise HTTPException(status_code=503, detail='The assistant could not complete this request.') from error
    if user and payload.chat_id:
        result['document_id'] = payload.document_id
        result['turn_id'] = save_chat_turn(
            payload.question.strip(),
            user['_id'],
            result,
            payload.chat_id,
        )
    result.pop('provider', None)
    result.pop('mode', None)
    result.pop('document_id', None)
    return result


@app.post('/upload')
async def upload(request: Request, file: UploadFile = File(...)):
    """Save a supported upload and index its chunks in Atlas and local Chroma."""
    allowed = {'.pdf', '.txt', '.md'}
    filename = clean_filename(file.filename)
    if Path(filename).suffix.lower() not in allowed:
        raise HTTPException(status_code=400, detail='Only PDF, TXT, and Markdown files are supported.')
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail='Files must be 10 MB or smaller.',
        )

    user = request_user(request)
    scope_user = user['_id'] if user else guest_workspace_id(request)
    try:
        rag_service.default_user_id = scope_user
        document_id = rag_service.document_id_for(filename, user_id=scope_user)
        indexed = rag_service.rebuild_index(
            user_id=scope_user,
            documents=[{
                'source': filename,
                'text': rag_service.read_uploaded_document(filename, contents),
                'user_id': scope_user,
                'document_id': document_id,
            }],
        )
        database = mongo_database()
        database['user_documents'].update_one(
            {'_id': document_id},
            {'$set': {
                'user_id': scope_user,
                'filename': filename,
                'chunks': indexed['chunks'],
                'updated_at': datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        if user:
            record_activity(scope_user, 'upload', filename, 'Ready to use in chat')
        return {
            'ok': True,
            'filename': filename,
            'document_id': document_id,
        }
    except PyMongoError as error:
        raise HTTPException(
            status_code=503,
            detail='Could not save document metadata in MongoDB.',
        ) from error
    except Exception as error:
        logger.exception('Could not upload and index document.')
        raise HTTPException(status_code=503, detail='The file could not be added to your library.') from error


if FRONTEND_DIR.is_dir():
    app.mount('/', StaticFiles(directory=FRONTEND_DIR, html=True), name='frontend')
