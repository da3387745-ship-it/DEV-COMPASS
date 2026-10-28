"""Local API for DevCompass document uploads, search, and chat."""
import hashlib, hmac, json, logging, os, re, secrets, shutil, threading, time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

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
DEBUG_RETRIEVAL = os.getenv('DEVCOMPASS_DEBUG_RETRIEVAL', '0').lower() in {'1', 'true', 'yes', 'on'}
PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_DIR.parent / 'frontend' / 'dist'
RAW_DIR = Path(os.getenv('DEVCOMPASS_RAW_DIR', PROJECT_DIR / 'data' / 'raw'))
CHROMA_DIR = Path(os.getenv('DEVCOMPASS_CHROMA_DIR', PROJECT_DIR / 'storage' / 'chroma'))
RAW_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_COOKIE, GUEST_COOKIE = 'devcompass_session', 'devcompass_guest'
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 7
PASSWORD_HASH_ITERATIONS = 310_000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_mongo_client = None


def debug_retrieval(msg):
    if DEBUG_RETRIEVAL:
        print(f"[RAG_DEBUG] {time.strftime('%H:%M:%S')} {msg}")


def create_embedding_model(name):
    from fastembed import TextEmbedding
    if name == 'all-MiniLM-L6-v2':
        name = 'sentence-transformers/all-MiniLM-L6-v2'
    return FastEmbeddingModel(TextEmbedding(model_name=name, cache_dir=os.getenv('FASTEMBED_CACHE_PATH'), threads=1))


def create_chroma_client(path):
    import chromadb
    return chromadb.PersistentClient(path=path)


class FastEmbeddingModel:
    def __init__(self, model):
        self.model = model

    def encode(self, texts):
        return [(e.tolist() if hasattr(e, 'tolist') else list(e)) for e in self.model.embed(texts, batch_size=16)]


app = FastAPI(title='DevCompass RAG Bridge')
app.add_middleware(CORSMiddleware, allow_origins=[
    'http://localhost:5173', 'http://127.0.0.1:5173', 'http://localhost:8001',
    'http://127.0.0.1:8001', 'null', 'https://dev-compass-1.onrender.com',
    'https://devcompass-frontend.onrender.com',
], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])


def secure_cookies_enabled():
    v = os.getenv('DEVCOMPASS_COOKIE_SECURE')
    return os.getenv('RENDER', '').lower() == 'true' if v is None else v.lower() in {'1', 'true', 'yes', 'on'}


def session_cookie_samesite():
    return 'none' if secure_cookies_enabled() else 'lax'


def get_mongo_client():
    global _mongo_client
    uri = os.getenv('MONGODB_URI', '').strip()
    if not uri:
        raise HTTPException(503, 'MongoDB is not configured. Add MONGODB_URI to the project .env file and restart DevCompass.')
    if _mongo_client is None:
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000, appname='DevCompass')
    return _mongo_client


def close_mongo_client():
    global _mongo_client
    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None


def mongo_database():
    return get_mongo_client()[os.getenv('MONGODB_DATABASE', 'devcompass').strip() or 'devcompass']


def ensure_auth_indexes():
    db = mongo_database()
    db['users'].create_index('email', unique=True)
    db['sessions'].create_index('expires_at', expireAfterSeconds=0)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PASSWORD_HASH_ITERATIONS)
    return f'pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split('$', 3)
        if algo != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def public_user(user):
    return {'email': user['email'], 'username': user.get('display_name', ''),
            'has_avatar': bool(user.get('avatar')),
            'settings': user.get('settings', {'theme': 'light', 'answer_style': 'concise'})}


def create_login_session(user, response):
    db = mongo_database()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db['sessions'].insert_one({'_id': hashlib.sha256(token.encode()).hexdigest(), 'user_id': user['_id'],
                               'created_at': now, 'expires_at': now + timedelta(seconds=SESSION_LIFETIME_SECONDS)})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_LIFETIME_SECONDS, httponly=True,
                        secure=secure_cookies_enabled(), samesite=session_cookie_samesite(), path='/')


def request_user(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        db = mongo_database()
        session = db['sessions'].find_one({'_id': hashlib.sha256(token.encode()).hexdigest(),
                                           'expires_at': {'$gt': datetime.now(timezone.utc)}})
        return db['users'].find_one({'_id': session['user_id']}) if session else None
    except PyMongoError as e:
        raise HTTPException(503, 'Could not verify the account session.') from e


def require_user(request):
    user = request_user(request)
    if not user:
        raise HTTPException(401, 'Please log in to access this account data.')
    return user


def guest_workspace_id(request):
    token = request.cookies.get(GUEST_COOKIE)
    if not token:
        raise HTTPException(401, 'Refresh the guest session and try again.')
    return f"guest-{hashlib.sha256(token.encode()).hexdigest()[:32]}"


def record_activity(user_id, activity_type, title, detail):
    mongo_database()['activity'].insert_one({'user_id': user_id, 'type': activity_type, 'title': title,
                                             'detail': detail, 'created_at': datetime.now(timezone.utc)})


def save_chat_turn(question, user_id, response, chat_id=None):
    try:
        db = mongo_database()
        turn_index = db['chat_history'].count_documents({'user_id': user_id, 'chat_id': chat_id}) if chat_id else None
        turn = {'_id': secrets.token_hex(16), 'user_id': user_id, 'question': question,
                'answer': response['answer'], 'sources': response.get('sources', []),
                'provider': response.get('provider'), 'mode': response.get('mode'),
                'created_at': datetime.now(timezone.utc)}
        if chat_id:
            turn.update(chat_id=chat_id, turn_index=turn_index)
        if response.get('document_id'):
            turn['document_id'] = response['document_id']
        result = db['chat_history'].insert_one(turn)
        if chat_id:
            update = {'$set': {'updated_at': datetime.now(timezone.utc)}}
            if turn_index == 0:
                update['$set']['title'] = question[:80] or 'New chat'
            db['chat_sessions'].update_one({'_id': chat_id, 'user_id': user_id}, update)
        record_activity(user_id, 'question', question[:120], 'Chat question answered')
        return str(result.inserted_id)
    except HTTPException:
        raise
    except (PyMongoError, ValueError) as e:
        raise HTTPException(503, 'Could not save this chat to MongoDB. Check the Atlas URI, network access, and database user.') from e


@app.on_event('shutdown')
def shutdown_mongodb():
    close_mongo_client()


class AskRequest(BaseModel):
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
    return re.sub(r'[^A-Za-z0-9._-]', '_', Path(filename or 'document').name)


def safe_user_folder(user_id):
    return re.sub(r'[^A-Za-z0-9_-]', '_', str(user_id))[:128] or 'default-user'


class AIProviderNotConfiguredError(RuntimeError):
    pass


# Constant sets used by RAGService.ask / needs_documents
GREETINGS = {'hi', 'hii', 'hiii', 'hy', 'hyy', 'hyyy', 'hey', 'heyy', 'heyyy', 'hello', 'helloo', 'hellooo',
             'hiya', 'good morning', 'good afternoon', 'good evening', 'how are you', 'how are you doing'}
DOC_TERMS = {'document', 'documents', 'doc', 'docs', 'file', 'files', 'pdf', 'notebook', 'notebooks', 'jupyter',
             'markdown', 'readme', 'source', 'citation', 'citations', 'repo', 'repository', 'codebase',
             'uploaded', 'attached', 'attachment'}
PROJECT_PHRASES = {'this code', 'the code', 'my code', 'this project', 'the project', 'my project', 'this system',
                   'the system', 'in this app', 'in this repo', 'in this repository', 'in this codebase',
                   'how does the system'}
FILE_PATTERNS = ('this pdf', 'that pdf', 'given pdf', 'uploaded pdf', 'attached pdf', 'this notebook', 'that notebook',
                 'uploaded notebook', 'attached notebook', 'this jupyter notebook', 'that jupyter notebook', 'this file',
                 'that file', 'given file', 'uploaded file', 'attached file', 'this document', 'that document',
                 'given document', 'uploaded document', 'this doc', 'that doc', 'given doc', 'uploaded doc',
                 'attached doc', 'the pdf', 'the file', 'the document', 'this report', 'that report', 'explain the pdf',
                 'explain this pdf', 'explain that pdf', 'explain the file', 'explain this file', 'explain that file',
                 'explain the document', 'explain this document', 'explain that document', 'summarize the pdf',
                 'summarise the pdf', 'summarize this pdf', 'summarize the file', 'summarize this file',
                 'summarize that file', 'what is in the pdf', 'what is in this pdf', 'what is in that pdf',
                 'what is in the file', 'what is in this file', 'what is in that file', 'from pdf', 'from this file',
                 'from this document', 'from that file')
FILE_TERMS = {'file', 'pdf', 'document', 'doc', 'report', 'notebook', 'jupyter'}
RELATED_TERMS = {'python', 'practice', 'roadmap', 'syllabus', 'journal', 'dev_compass', 'ai', 'csi'}


class RAGService:
    def __init__(self):
        self._embedding_model = None
        self._chroma_client = None
        self._collection = None
        self._resource_lock = threading.Lock()
        self.collection_name = 'devcompass_documents'
        self.vector_collection_name = os.getenv('MONGODB_VECTOR_COLLECTION', 'document_chunks').strip() or 'document_chunks'
        self.vector_index_name = os.getenv('MONGODB_VECTOR_INDEX', 'devcompass_vector').strip() or 'devcompass_vector'
        self.vector_dimensions = 384
        self.default_user_id = os.getenv('DEVCOMPASS_USER_ID', 'default-user')
        gk, grk, ork = os.getenv('GEMINI_API_KEY'), os.getenv('GROQ_API_KEY'), os.getenv('OPENROUTER_API_KEY')
        self.gemini_client = genai.Client(api_key=gk) if gk else None
        self.groq_client = Groq(api_key=grk) if grk else None
        self.openrouter_client = OpenAI(api_key=ork, base_url='https://openrouter.ai/api/v1') if ork else None

    @property
    def embedding_model(self):
        if self._embedding_model is None:
            with self._resource_lock:
                if self._embedding_model is None:
                    self._embedding_model = create_embedding_model(os.getenv('EMBEDDING_MODEL', 'all-MiniLM-L6-v2'))
        return self._embedding_model

    @property
    def collection(self):
        if self._collection is None:
            with self._resource_lock:
                if self._collection is None:
                    self._chroma_client = create_chroma_client(str(CHROMA_DIR))
                    self._collection = self._chroma_client.get_or_create_collection(name=self.collection_name)
        return self._collection

    @property
    def vector_collection(self):
        return mongo_database()[self.vector_collection_name]

    def document_id_for(self, source_name, user_id=None):
        return hashlib.sha1(f"{user_id or self.default_user_id}:{source_name}".encode()).hexdigest()

    def effective_user_id(self, user_id=None):
        return user_id or self.default_user_id

    def active_document_id(self, user_id=None, document_id=None):
        if document_id:
            return document_id
        uid = self.effective_user_id(user_id)
        docs = self.load_documents(user_id=uid)
        if docs:
            return max(docs, key=lambda d: Path(d['path']).stat().st_mtime)['document_id']
        if os.getenv('MONGODB_URI', '').strip():
            doc = next(iter(self._mongo_document_metadata(uid).sort('updated_at', -1).limit(1)), None)
            if doc:
                return doc['_id']
        return None

    def _mongo_document_metadata(self, user_id):
        return mongo_database()['user_documents'].find({'user_id': user_id}, {'filename': 1, 'updated_at': 1})

    def read_document(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == '.pdf':
            return '\n'.join(p.extract_text() or '' for p in PdfReader(str(path)).pages).strip()
        if suffix == '.ipynb':
            return self.read_notebook(path.read_bytes())
        return path.read_text(encoding='utf-8', errors='ignore')

    def read_uploaded_document(self, filename, contents):
        suffix = Path(filename).suffix.lower()
        if suffix == '.pdf':
            return '\n'.join(p.extract_text() or '' for p in PdfReader(BytesIO(contents)).pages).strip()
        if suffix == '.ipynb':
            return self.read_notebook(contents)
        return contents.decode('utf-8', errors='ignore')

    def read_notebook(self, contents):
        try:
            nb = json.loads(contents.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError('The Jupyter notebook is not valid UTF-8 JSON.') from e
        if not isinstance(nb, dict) or not isinstance(nb.get('cells'), list):
            raise ValueError('The Jupyter notebook does not contain a valid cells list.')
        cells = []
        for i, cell in enumerate(nb['cells'], 1):
            if not isinstance(cell, dict) or cell.get('cell_type') not in {'markdown', 'code'}:
                continue
            src = cell.get('source', '')
            if isinstance(src, list):
                src = ''.join(p for p in src if isinstance(p, str))
            if isinstance(src, str) and src.strip():
                cells.append(f"[{'Markdown' if cell['cell_type'] == 'markdown' else 'Code'} cell {i}]\n{src.strip()}")
        return '\n\n'.join(cells)

    def load_documents(self, user_id=None):
        files = sorted(p for p in RAW_DIR.rglob('*') if p.suffix.lower() in {'.pdf', '.txt', '.md', '.ipynb'})
        if user_id:
            udir = RAW_DIR / safe_user_folder(user_id)
            files = [p for p in files if udir in p.parents]
        docs, active = [], self.effective_user_id(user_id)
        for path in files:
            try:
                text = self.read_document(path)
            except (OSError, PdfReadError, ValueError) as e:
                logger.warning('Skipping unreadable document %s (%s).', path.name, type(e).__name__)
                continue
            if text.strip():
                docs.append({'source': path.name, 'path': str(path), 'text': text, 'user_id': active,
                             'document_id': self.document_id_for(path.name, user_id=active)})
        return docs

    def chunk_text(self, text, size=500, overlap=50):
        clean = ' '.join(text.split())
        chunks, start = [], 0
        while start < len(clean):
            end = min(start + size, len(clean))
            piece = clean[start:end].strip()
            if piece:
                chunks.append(piece)
            if end == len(clean):
                break
            start = max(end - overlap, start + 1)
        return chunks

    def rebuild_index(self, user_id=None, documents=None):
        uid = self.effective_user_id(user_id)
        documents = self.load_documents(user_id=uid) if documents is None else documents
        chunks = []
        for doc in documents:
            for n, text in enumerate(self.chunk_text(doc['text'])):
                chunks.append({'id': hashlib.sha1(f"{doc['user_id']}:{doc['document_id']}:{n}:{text}".encode()).hexdigest(),
                               'text': text, 'source': doc['source'], 'document_id': doc['document_id'],
                               'user_id': doc['user_id'], 'chunk': n})
        embeddings = []
        if chunks:
            embeddings = self.embedding_model.encode([c['text'] for c in chunks])
            if hasattr(embeddings, 'tolist'):
                embeddings = embeddings.tolist()
        atlas = None
        if os.getenv('MONGODB_URI', '').strip():
            if any(len(e) != self.vector_dimensions for e in embeddings):
                raise ValueError(f'The configured embedding model must produce {self.vector_dimensions}-dimensional vectors for Atlas Vector Search.')
            atlas = self.vector_collection
            ops = [ReplaceOne({'_id': c['id']}, {'_id': c['id'], 'embedding': [float(v) for v in e], 'text': c['text'],
                   'source': c['source'], 'document_id': c['document_id'], 'user_id': c['user_id'], 'chunk': c['chunk']}, upsert=True)
                   for c, e in zip(chunks, embeddings)]
            for i in range(0, len(ops), 100):
                atlas.bulk_write(ops[i:i + 100], ordered=False)
        existing = self.collection.get(
            where={'user_id': {'$eq': uid}} if documents is None else
            {'$and': [{'user_id': {'$eq': uid}}, {'document_id': {'$in': [d['document_id'] for d in documents]}}]},
        ).get('ids', [])
        if existing:
            self.collection.delete(ids=existing)
        if documents is None:
            if atlas is not None:
                atlas.delete_many({'user_id': uid, **({'_id': {'$nin': [c['id'] for c in chunks]}} if chunks else {})})
        elif atlas is not None:
            doc_ids = [d['document_id'] for d in documents]
            if doc_ids:
                atlas.delete_many({'user_id': uid, 'document_id': {'$in': doc_ids},
                                   **({'_id': {'$nin': [c['id'] for c in chunks]}} if chunks else {})})
        if chunks:
            self.collection.upsert(
                ids=[c['id'] for c in chunks], documents=[c['text'] for c in chunks],
                metadatas=[{'source': c['source'], 'document_id': c['document_id'],
                            'user_id': c['user_id'], 'chunk': c['chunk']} for c in chunks],
                embeddings=embeddings)
        if atlas is not None:
            db = mongo_database()
            for d in documents:
                db['user_documents'].update_one({'_id': d['document_id']},
                    {'$set': {'user_id': uid, 'filename': d['source'], 'chunks': len(self.chunk_text(d['text'])),
                              'updated_at': datetime.now(timezone.utc)}}, upsert=True)
        if not chunks:
            return {'documents': len(documents), 'chunks': 0, 'document_id': None}
        return {'documents': len(documents), 'chunks': len(chunks), 'document_id': self.active_document_id(user_id=uid)}

    def retrieve(self, question, limit=5, max_distance=2.0, user_id=None, document_id=None):
        uid = self.effective_user_id(user_id)
        filters = [{'user_id': {'$eq': uid}}]
        if document_id:
            filters.append({'document_id': {'$eq': document_id}})
        scope = {'$and': filters}
        q_emb = self.embedding_model.encode([question])
        if hasattr(q_emb, 'tolist'):
            q_emb = q_emb.tolist()
        if os.getenv('MONGODB_URI', '').strip():
            try:
                vector = [float(v) for v in q_emb[0]]
                if len(vector) != self.vector_dimensions:
                    raise ValueError(f'The configured embedding model must produce {self.vector_dimensions}-dimensional vectors for Atlas Vector Search.')
                fq = {'user_id': {'$eq': uid}}
                if document_id:
                    fq['document_id'] = {'$eq': document_id}
                res = list(self.vector_collection.aggregate([
                    {'$vectorSearch': {'index': self.vector_index_name, 'path': 'embedding', 'queryVector': vector,
                                       'numCandidates': min(max(limit * 20, 100), 10000), 'limit': limit, 'filter': fq}},
                    {'$project': {'text': 1, 'source': 1, 'chunk': 1, 'score': {'$meta': 'vectorSearchScore'}}}]))
                matches = []
                for item in res:
                    d = 1 - float(item.get('score', 0))
                    if d <= max_distance:
                        matches.append({'text': item.get('text', ''), 'source': item.get('source', 'unknown'),
                                        'chunk': item.get('chunk', 0), 'distance': round(d, 4)})
                if matches:
                    return matches
            except (PyMongoError, HTTPException) as e:
                logger.warning('Atlas Vector Search failed; falling back to local Chroma (%s).', type(e).__name__)
        debug_retrieval(f'collection={self.collection_name} user_id={uid} document_id={document_id} topK={limit} threshold={max_distance}')
        result = self.collection.query(query_embeddings=q_emb, n_results=limit, where=scope,
                                       include=['documents', 'metadatas', 'distances'])
        texts = result.get('documents', [[]])[0]
        metas = result.get('metadatas', [[]])[0]
        dists = result.get('distances', [[]])[0]
        matches = []
        for text, meta, dist in zip(texts, metas, dists):
            try:
                v = float(dist)
            except (TypeError, ValueError):
                continue
            if v <= max_distance:
                matches.append({'text': text, 'source': meta.get('source', 'unknown'),
                                'chunk': meta.get('chunk', 0), 'distance': round(v, 4)})
        debug_retrieval(f"collection={self.collection_name} result_count={len(matches)} sources={[m['source'] for m in matches]}")
        return matches

    def retrieve_all_documents(self, user_id=None, document_id=None):
        uid = self.effective_user_id(user_id)
        filters = [{'user_id': {'$eq': uid}}]
        if document_id:
            filters.append({'document_id': {'$eq': document_id}})
        scope = {'$and': filters}
        if os.getenv('MONGODB_URI', '').strip():
            try:
                docs = self.vector_collection.find(
                    {'user_id': uid, **({'document_id': document_id} if document_id else {})},
                    {'text': 1, 'source': 1, 'chunk': 1}).sort('chunk', 1).limit(5)
                matches = [{'text': i['text'], 'source': i.get('source', 'unknown'),
                            'chunk': i.get('chunk', 0), 'distance': 0} for i in docs if i.get('text')]
                if matches:
                    return matches
            except (PyMongoError, HTTPException) as e:
                logger.warning('Could not read Atlas document chunks; falling back to local Chroma (%s).', type(e).__name__)
        result = self.collection.get(include=['documents', 'metadatas'], where=scope)
        return [{'text': t, 'source': m.get('source', 'unknown'), 'chunk': m.get('chunk', 0), 'distance': 0}
                for t, m in zip(result.get('documents', []), result.get('metadatas', [])) if t]

    def keyword_search(self, question, limit=5, user_id=None, document_id=None):
        uid = self.effective_user_id(user_id)
        docs = self.load_documents(user_id=uid)
        if document_id:
            docs = [d for d in docs if d['document_id'] == document_id]
        words = [w.lower() for w in re.findall(r'[A-Za-z0-9]+', question) if len(w) > 2]
        if not words:
            return []
        scored = [(sum(1 for w in words if w in d['text'].lower()), d) for d in docs]
        scored = [s for s in scored if s[0]]
        results = []
        for _, doc in sorted(scored, key=lambda r: r[0], reverse=True)[:3]:
            for i, chunk in enumerate(self.chunk_text(doc['text'])):
                if any(w in chunk.lower() for w in words):
                    results.append({'text': chunk, 'source': doc['source'], 'chunk': i, 'distance': 0})
                    if len(results) >= limit:
                        return results
        if results or not os.getenv('MONGODB_URI', '').strip():
            return results
        try:
            atlas_chunks = self.vector_collection.find(
                {'user_id': uid, **({'document_id': document_id} if document_id else {})},
                {'text': 1, 'source': 1, 'chunk': 1})
            matching = []
            for item in atlas_chunks:
                text = item.get('text', '')
                score = sum(1 for w in words if w in text.lower())
                if score:
                    matching.append((score, item))
            matching.sort(key=lambda r: r[0], reverse=True)
            return [{'text': i['text'], 'source': i.get('source', 'unknown'),
                     'chunk': i.get('chunk', 0), 'distance': 0} for _, i in matching[:limit]]
        except (PyMongoError, HTTPException) as e:
            logger.warning('Atlas keyword fallback failed (%s).', type(e).__name__)
        return results

    def build_prompt(self, question, matches, response_style='concise'):
        context = '\n\n'.join(f"Source: {m['source']} (chunk {m['chunk']})\n{m['text']}" for m in matches)
        guidance = 'Give a concise, practical answer.' if response_style == 'concise' else 'Give a thorough, well-structured explanation with useful details.'
        return ('You are DevCompass, a careful technical documentation assistant.\n\n'
                'Use only the supplied context to answer this document question. Do not guess, fill gaps, or use general knowledge to create a confident-sounding '
                "answer. If the context does not clearly support the answer, say: 'I could not find that in the uploaded documents.' "
                f'{guidance} Mention the source filename only when the context supports the claim.\n\nContext:\n{context}\n\nQuestion: {question}')

    def build_chat_prompt(self, question, response_style='concise'):
        guidance = 'Keep the answer concise and practical.' if response_style == 'concise' else 'Give a thorough, well-structured explanation with useful details.'
        return ('You are DevCompass, a friendly and practical AI assistant for developers.\n'
                f'Have a normal conversation and answer naturally. {guidance} Do not pretend you searched documents and do not invent citations.\n\nUser: {question}')

    def ask_gemini(self, prompt):
        if not self.gemini_client:
            raise RuntimeError('GEMINI_API_KEY is not configured')
        return self.gemini_client.models.generate_content(model=os.getenv('GEMINI_MODEL', 'gemini-2.0-flash'), contents=prompt).text

    def ask_openai_style(self, client, model, prompt):
        if not client:
            raise RuntimeError('Provider key is not configured')
        return client.chat.completions.create(model=model, messages=[{'role': 'user', 'content': prompt}], temperature=0.1).choices[0].message.content

    def has_any_provider_key(self):
        return any((os.getenv('GEMINI_API_KEY'), os.getenv('GROQ_API_KEY'), os.getenv('OPENROUTER_API_KEY')))

    def call_providers(self, prompt):
        if not self.has_any_provider_key():
            raise AIProviderNotConfiguredError('No AI provider is configured. Add GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY before enabling chat responses.')
        providers = [
            ('gemini', lambda: self.ask_gemini(prompt)),
            ('groq', lambda: self.ask_openai_style(self.groq_client, os.getenv('GROQ_MODEL', 'llama-3.1-8b-instant'), prompt)),
            ('openrouter', lambda: self.ask_openai_style(self.openrouter_client, os.getenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini'), prompt)),
        ]
        errors = []
        for name, call in providers:
            try:
                return call(), name
            except Exception as e:
                errors.append(f'{name}: {e}')
        raise RuntimeError('All model providers failed. ' + ' | '.join(errors))

    def needs_documents(self, question):
        q = f" {' '.join(re.findall(r'[a-z0-9]+', question.lower()))} "
        return any(f" {' '.join(re.findall(r'[a-z0-9]+', t))} " in q for t in DOC_TERMS) or \
               any(f" {p} " in q for p in PROJECT_PHRASES)

    def asks_for_uploaded_content(self, question):
        q = question.lower()
        if any(t in q for t in FILE_PATTERNS):
            return True
        tokens = re.findall(r'[a-z0-9]+', q)
        return any(v in tokens for v in ('explain', 'summarize', 'describe')) and any(t in FILE_TERMS for t in tokens)

    def resolve_ambiguous_file_question(self, question, user_id=None, document_id=None):
        uid = self.effective_user_id(user_id)
        docs = self.load_documents(user_id=uid)
        if not docs:
            if not os.getenv('MONGODB_URI', '').strip():
                return []
            indexed = list(self._mongo_document_metadata(uid).sort('updated_at', -1))
            if not indexed:
                return []
            selected = next((i for i in indexed if i.get('_id') == document_id or i.get('filename', '').lower() in question.lower()), indexed[0])
            return self.retrieve_all_documents(user_id=uid, document_id=document_id or selected['_id'])
        active = None
        requested = document_id or self.active_document_id(user_id=user_id)
        if requested:
            active = next((d for d in docs if d['document_id'] == requested), None)
        if active is None:
            active = max(docs, key=lambda i: Path(i['path']).stat().st_mtime)
        ql = question.lower()
        explicit = []
        for d in docs:
            stem = Path(d['source']).stem.lower()
            norm = re.sub(r'[^a-z0-9]+', ' ', stem).strip()
            names = {Path(d['source']).name.lower(), stem, norm, norm.replace(' ', '_'), norm.replace(' ', '-')}
            if any(n in ql for n in names):
                explicit.append(d)
        if explicit:
            exact = [d for d in explicit if Path(d['source']).name.lower() in ql]
            chosen = exact[0] if exact else explicit[0]
            return [{'text': c, 'source': chosen['source'], 'chunk': i, '