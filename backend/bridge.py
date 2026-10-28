"""Local API for DevCompass document uploads, search, and chat."""

import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from pypdf.errors import PdfReadError

from .rag import (
    AIProviderNotConfiguredError,
    DEBUG_RETRIEVAL,
    FastEmbeddingModel,
    RAGService as _RAGService,
    create_chroma_client,
    create_embedding_model,
    debug_retrieval,
    safe_user_folder,
)

logger = logging.getLogger(__name__)
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
        'https://devcompass-frontend.onrender.com',
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


class RAGService(_RAGService):
    """Wire the RAG engine to this API module's storage and test seams."""

    def __init__(self):
        super().__init__(
            raw_dir=RAW_DIR,
            chroma_dir=CHROMA_DIR,
            mongo_database=lambda: mongo_database(),
            embedding_model_factory=create_embedding_model,
            chroma_client_factory=create_chroma_client,
            safe_user_folder=safe_user_folder,
            logger=logger,
            debug_retrieval=debug_retrieval,
        )


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
        logger.error('MongoDB account signup failed (%s).', type(error).__name__)
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
        logger.error('MongoDB account login failed (%s).', type(error).__name__)
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
    allowed = {'.pdf', '.txt', '.md', '.ipynb'}
    filename = clean_filename(file.filename)
    if Path(filename).suffix.lower() not in allowed:
        raise HTTPException(
            status_code=400,
            detail='Only PDF, TXT, Markdown, and Jupyter Notebook files are supported.',
        )
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail='Files must be 10 MB or smaller.',
        )

    user = request_user(request)
    scope_user = user['_id'] if user else guest_workspace_id(request)
    try:
        text = rag_service.read_uploaded_document(filename, contents)
    except (PdfReadError, ValueError) as error:
        logger.info('Rejected unreadable upload %s (%s).', filename, type(error).__name__)
        raise HTTPException(
            status_code=422,
            detail='Could not parse this file. Check that it is a valid PDF or Jupyter notebook.',
        ) from error
    if not text.strip():
        detail = (
            'This PDF contains no selectable text. Scanned/image-only PDFs need OCR, '
            'which is not supported yet.'
            if Path(filename).suffix.lower() == '.pdf'
            else 'This file contains no readable text, Markdown, or notebook code cells.'
        )
        raise HTTPException(status_code=422, detail=detail)

    try:
        rag_service.default_user_id = scope_user
        document_id = rag_service.document_id_for(filename, user_id=scope_user)
        indexed = rag_service.rebuild_index(
            user_id=scope_user,
            documents=[{
                'source': filename,
                'text': text,
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
