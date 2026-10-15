# DevCompass

DevCompass is a browser-based developer assistant that can run locally or as a
public Render web service. Its frontend is a static single-page application. A Python FastAPI service handles accounts, document
uploads, retrieval-augmented generation (RAG), model-provider calls, and
MongoDB persistence.

This document describes the current implementation, how its parts connect, and
how to run and test it. It is based on the code in this repository; it does not
assume a separate notebook or build system.

## Architecture at a glance

```text
Browser
  React (frontend/src/main.jsx) + existing page logic (frontend/app.js)
       | bundled by esbuild into frontend/dist/
       | HTTP JSON / multipart upload; session cookies
       v
FastAPI (127.0.0.1:8000)
  backend/bridge.py
       |-- MongoDB Atlas: accounts, chat, metadata, document chunks + vectors
       |-- Local files: legacy documents under backend/data/raw/
       |-- ChromaDB: temporary local mirror during Atlas verification
       |-- Sentence Transformers: local text embeddings
       `-- Gemini -> Groq -> OpenRouter: configured LLM fallback

Static file server (local) or FastAPI (Render) serves frontend/dist/
```

MongoDB is accessed only by the backend. The browser sends API requests to the
same hostname on port `8000`, includes the session cookie, and never receives
the MongoDB URI or model-provider API keys. The backend CORS policy allows the
current local frontend origins on ports `5173` and `8001`, plus `null`.

## Project layout

```text
DEV-COMPASS/
|-- README.md                         This architecture and run guide
|-- .env                              Local secrets and configuration (not for sharing)
|-- .venv/                            Local Python virtual environment
|-- .gitignore                        Local ignore rules
|-- .githooks/
|   `-- post-commit                   Pushes a commit to the configured GitHub remote
|-- .github/
|   `-- workflows/ci.yml              Frontend build and backend tests on push/PR
|-- render.yaml                       Render deployment configuration
|-- frontend/
|   |-- index.html                    SPA document shell
|   |-- app.js                        Existing routes, page templates, and API interactions
|   |-- styles.css                    Layout, components, and themes
|   |-- package.json                  React and esbuild build scripts
|   |-- package-lock.json             Locked frontend dependencies
|   |-- build.mjs                     Bundles the app and copies static assets
|   |-- src/main.jsx                  React mount and compatibility renderer
|   `-- logo.png                      Frontend image asset
|-- backend/
|   |-- __init__.py                   Makes backend importable by Uvicorn/tests
|   |-- bridge.py                     FastAPI routes, MongoDB, RAG, and model calls
|   |-- check_mongodb_connection.py   Safe standalone MongoDB ping, used by tests
|   |-- data/raw/                     Legacy local source files, separated by account
|   |-- storage/chroma/               Temporary local vector mirror (migration only)
`-- other/
    |-- start_app.py                  Starts or reuses the local services
    |-- requirements.txt              Python dependencies
    |-- .env.example                  Safe environment-variable template
    |-- error_log.md                  Persistent startup and configuration notes
    |-- tests/
    |   |-- test_mongodb_connection.py MongoDB/account API tests
    |   `-- test_rag_scope.py         Retrieval and account-scope tests
```

The project is kept in three main folders: `frontend/` for browser code,
`backend/` for the API and persistent app data, and `other/` for support files,
tests, and setup. Root-level `README.md`, `.gitignore`, and `render.yaml` stay at the root because
they document or configure the project.
The root `.env` and `.venv` stay outside the source folders so the backend can
load local secrets and use the existing Python environment. `frontend/dist/`,
`frontend/node_modules/`, and Python `__pycache__/` directories are generated
locally and are not committed.

The React entry point owns the app root and renders the existing page templates
through a compatibility renderer. This keeps the current routes, API calls,
event handlers, and page behavior unchanged while moving the frontend build to
React and esbuild; the existing page templates have not yet been rewritten as
individual JSX components.

## Runtime and connection flow

1. Install Node.js, then run `npm install --prefix frontend` once. Run
   `other/start_app.py` from the project root. It builds the React frontend,
   starts Uvicorn with `backend.bridge:app` on `127.0.0.1:8000`, and serves
   `frontend/dist/` on
   `127.0.0.1:8001`. It checks the API routes and frontend page before reporting
   that a service is ready. A healthy service already using a port is reused;
   an unrelated or outdated listener is reported instead of being stopped.
2. `frontend/src/main.jsx` mounts the React application; the existing
   `frontend/app.js` renders the current hash route and calls the API using
   `fetch`. Requests include cookies. The API hostname follows the browser
   hostname, so `localhost` and `127.0.0.1` remain consistent for cookies.
3. FastAPI reads configuration from the root `.env`. Normal application
   database operations reuse one lazily created PyMongo client, which is closed
   when the API shuts down. Database-status checks use separate short-lived
   clients and close them after the check.
4. New uploads are size-limited, parsed in memory, embedded locally, and indexed
   in both Atlas Vector Search and Chroma during migration. Atlas stores
   searchable text chunks, vectors, and per-account metadata; original uploads
   are not retained on the server.
5. Chat prompts and retrieved passages are sent from the backend to the first
   configured LLM provider that succeeds. The resulting answer and metadata
   are written to MongoDB.

### Main data pipelines

#### Account and profile

```text
Signup/login form
  -> POST /auth/signup or /auth/login
  -> MongoDB users + sessions
  -> HttpOnly devcompass_session cookie
  -> subsequent protected requests resolve the session to the user
```

Signup normalizes the email and stores a salted PBKDF2-SHA256 password hash.
Signup requires a password of at least 12 characters. The session token is
returned as an HttpOnly, SameSite=Lax cookie with a seven-day lifetime; only its
SHA-256 digest is stored in the `sessions` collection. Profile changes update
the user's username (stored as `display_name` for compatibility), settings,
and optional profile image in `users`.

Guest document scope is derived from a random HttpOnly cookie on the backend;
the browser does not choose or receive a workspace/account ID. Authenticated
document and chat operations use the account resolved from the session cookie.

### Deploy on Render Free

The root `render.yaml` defines a Render Free FastAPI web service that serves
both the API and frontend. Free services have an ephemeral filesystem and spin
down when idle; this configuration does not use a persistent disk. New document
chunks, embeddings, and metadata are stored in Atlas so they survive restarts.
The service is intended for hobby use, not production availability. The local
embedding model may exceed free-instance memory limits; monitor deployment
health and upgrade or use a lower-memory embedding provider if it restarts.

1. Push this project to a GitHub repository that does not contain `.env` or
   other credentials.
2. In Render, create a Blueprint and connect that repository. Render reads
   `render.yaml` and prompts for `MONGODB_URI` and provider API keys. Enter
   secrets in Render only; at least one provider key is needed for AI answers.
3. In MongoDB Atlas, allow the Render service's outbound IP addresses through
   Network Access, and confirm the database user in `MONGODB_URI` has access to
   the configured database.
4. Confirm the Atlas Vector Search index `devcompass_vector` is ready on the
   `document_chunks` collection, then deploy the Blueprint. Render provides a
   public `https://` URL when the service is live; use that URL from any
   internet-connected browser.

The local `other/start_app.py` launcher builds the frontend for development.
Render builds the same frontend as part of deployment; FastAPI serves its
generated assets from `frontend/dist/`.

The UI uses hash navigation (`#/home`, `#/dashboard`, `#/chat`, `#/settings`,
`#/docs`, `#/welcome`, `#/login`, and `#/signup`). `app.js` builds page content
from JavaScript templates and handles forms and buttons through delegated DOM
events; `index.html` is the static mount point.

#### Upload and indexing

```text
Browser file form
  -> POST /upload (multipart/form-data)
  -> validate PDF/TXT/Markdown extension and 10 MB limit
  -> extract text in memory (pypdf for PDF; UTF-8 text for TXT/Markdown)
  -> split into 500-character passages with 50-character overlap
  -> embed passages with Sentence Transformers
  -> upsert passage, vector, source, and ownership in Atlas document_chunks
  -> mirror vectors in local Chroma during migration
  -> update MongoDB user_documents and activity
```

Atlas chunks include the source name, account ID, document ID, chunk number,
text, and 384-dimensional embedding. The `devcompass_vector` index uses cosine
similarity and prefilters by `user_id` and `document_id`. New uploads are
indexed for only the current document; the app does not silently migrate other
local files. Legacy files under `backend/data/raw/` remain local until
explicitly re-uploaded or migrated.

#### Question and answer

```text
Chat prompt
  -> POST /ask
  -> choose the account/workspace and response style
  -> route greeting/general chat vs document question
  -> for document questions: retrieve passages from Atlas Vector Search
       -> use local Chroma only as a temporary fallback during migration
       -> broad file request: select named/active file passages
       -> otherwise: semantic top-5 search, then keyword fallback if needed
  -> construct a context-limited prompt for document answers
  -> try Gemini, then Groq, then OpenRouter; return the first successful call
  -> return a formatted answer and source filenames
  -> save each question/answer as a turn in its parent chat session
```

Each signed-in chat has one document in `chat_sessions`; its messages are
ordered turns in `chat_history`. Creating a new chat creates a new session,
while sending another message continues the active session. Older one-message
history records are grouped into sessions when chat history is first loaded.
Prompt edits regenerate and replace the selected turn's response.

The RAG service uses `all-MiniLM-L6-v2` by default for embeddings, keeps up to
five semantic matches, and filters out distances above `2.0` by default.
It chooses document RAG when a question explicitly refers to a file, document,
repository, codebase, or project; ordinary factual and technical questions go
to general chat instead. Intent matching uses whole words and phrases, so a word
like “capital” does not accidentally match “API.” Greetings have a local
response. General-chat prompts do not claim to use document sources. Document
prompts instruct the model not to invent facts when the uploaded context is
insufficient.
Each `/ask` request is built from the current question and selected document;
saved chat history is available for display, but prior turns are not
automatically added to the model prompt as conversational context.

## MongoDB collections

The default database name is `devcompass`; override it with
`MONGODB_DATABASE`.

| Collection | What the application stores |
| --- | --- |
| `users` | Normalized email, display name, password hash, preferences, creation time |
| `sessions` | Hashed session token as the MongoDB key, user ID, creation/expiry times |
| `chat_sessions` | Account ID, editable title, creation/update times |
| `chat_history` | Account ID, chat ID, ordered question/answer turns, sources, provider, mode |
| `activity` | Account/workspace ID, event type, title/detail, time |
| `user_documents` | Account/workspace ID, document metadata, indexed chunk count, update time |
| `document_chunks` | Searchable text, 384-dimensional embedding, source and ownership metadata |

## HTTP API

The API is defined in `backend/bridge.py` and serves interactive documentation
at `http://127.0.0.1:8000/docs` while running.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/auth/signup` | Create an account and start a session |
| `POST` | `/auth/login` | Verify credentials and start a session |
| `GET` | `/auth/me` | Check the current session |
| `POST` | `/auth/logout` | Delete the current session and cookie |
| `GET` | `/guest/session` | Start a backend-scoped guest document session |
| `GET` | `/chat/history` | List the signed-in user's chat sessions |
| `POST` | `/chat/session` | Create a chat session |
| `GET` | `/chat/session/{chat_id}` | Load turns in one owned chat |
| `PATCH` | `/chat/session/{chat_id}` | Rename an owned chat |
| `DELETE` | `/chat/session/{chat_id}` | Delete an owned chat and its turns |
| `PUT` | `/chat/session/{chat_id}/turn/{turn_id}` | Edit a prompt and regenerate its answer |
| `POST` | `/profile/avatar` | Save an authenticated profile image |
| `DELETE` | `/profile` | Delete an account after password confirmation |
| `GET` | `/profile` | Read the signed-in user's profile |
| `PUT` | `/profile` | Save display name and preferences |
| `GET` | `/dashboard` | Return account counts and recent activity |
| `GET` | `/chat/history` | Return bounded chat history for the signed-in user |
| `GET` | `/documents` | List workspace documents from local files and MongoDB metadata |
| `POST` | `/upload` | Parse and index a PDF, TXT, or Markdown file (up to 10 MB) |
| `POST` | `/ask` | Answer a general or document-grounded question |
| `GET` | `/status` | Report whether the Atlas vector index is ready |
| `GET` | `/database/status` | Ping MongoDB and report connection status |

Profile, account data, dashboard, and chat-history endpoints require a valid
session. `/auth/me` can be called without a session. Document, upload, and ask
routes use the authenticated account ID or a backend-generated guest scope.
They do not accept account IDs from the browser.

## Configuration

Create the root `.env` file yourself. `other/.env.example` contains empty
MongoDB placeholders; add the provider keys you plan to use. Never put
credentials in frontend files or share the real `.env`.

```dotenv
MONGODB_URI=mongodb+srv://<database-user>:<url-encoded-password>@<cluster-address>/
MONGODB_DATABASE=devcompass
MONGODB_VECTOR_COLLECTION=document_chunks
MONGODB_VECTOR_INDEX=devcompass_vector

# Configure at least one model provider:
GEMINI_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
```

Optional settings read by the current service:

| Variable | Purpose | Default |
| --- | --- | --- |
| `EMBEDDING_MODEL` | Sentence Transformers model name | `all-MiniLM-L6-v2` |
| `MONGODB_VECTOR_COLLECTION` | Atlas collection holding chunk vectors | `document_chunks` |
| `MONGODB_VECTOR_INDEX` | Atlas Vector Search index name | `devcompass_vector` |
| `GEMINI_MODEL` | Gemini model | `gemini-2.0-flash` |
| `GROQ_MODEL` | Groq chat model | `llama-3.1-8b-instant` |
| `OPENROUTER_MODEL` | OpenRouter chat model | `openai/gpt-4o-mini` |
| `DEVCOMPASS_COOKIE_SECURE` | Mark session cookie secure for HTTPS | `0` |
| `DEVCOMPASS_USER_ID` | Default local workspace when none is supplied | `default-user` |
| `DEVCOMPASS_DEBUG_RETRIEVAL` | Enable local retrieval diagnostic logging | `0` |

The API's `/database/status` route and the standalone connection-check script
use the URI from the root `.env`, make a ping with a separate short-lived client,
and close that client afterwards. Normal application queries reuse the shared
MongoDB client for the process lifetime.

## Setup and run on Windows

From PowerShell, change to the project root, then create or activate the existing
virtual environment and install the declared dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r other\requirements.txt
```

If the environment already exists, activate it and install/update dependencies:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r other\requirements.txt
```

Start both services from the project root:

```powershell
.\.venv\Scripts\python.exe other\start_app.py
```

Open `http://127.0.0.1:8001/`. The API runs at `http://127.0.0.1:8000/`.
Starting from the project root is important because `.env` is loaded from the
current working directory. Set at least one of `GEMINI_API_KEY`, `GROQ_API_KEY`,
or `OPENROUTER_API_KEY` in `.env` to enable model-generated answers; guest chat
and document uploads do not require an account. Install Node.js and run
`npm install --prefix frontend` from the project root before starting the app;
the launcher builds the React frontend on each startup. First startup may
download the embedding model.

To check MongoDB without starting the app:

```powershell
.\.venv\Scripts\python.exe backend\check_mongodb_connection.py
```

The helper prints only success or a safe error category; it never prints the
connection string. The running API's database status is available at
`http://127.0.0.1:8000/database/status`.

## Dependencies and development checks

`other/requirements.txt` declares the Python runtime packages for the API,
MongoDB, PDF parsing, local embeddings, and the temporary Chroma mirror. The
frontend uses React and esbuild, with its npm dependencies declared in
`frontend/package.json`.

Run the unit tests from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s other\tests -v
```

The current tests cover password/session behavior, profile and dashboard
persistence, safe account-data projections, chat saving/history, MongoDB
connection reporting, and account/document retrieval scoping.

Build the frontend and check the existing page-logic JavaScript syntax:

```powershell
npm run build --prefix frontend
node --check frontend\app.js
```

The tests use mocks/fakes for MongoDB and ChromaDB behavior. The standalone
MongoDB connection helper performs a real Atlas ping only when you run it
explicitly with a configured `.env`.

## GitHub commits and checks

The `CI` GitHub Actions workflow builds the frontend and runs the backend unit
tests on every push and pull request. Configure Git to use the included
post-commit hook if you want each local commit to push its current branch to
GitHub automatically. A rejected push or missing remote is reported in the
Git output; the local commit is not discarded.

This project has its own Git repository rooted here, separate from the repository
in its parent directory. The current branch is `main`, and the automatic-push
hook is enabled for this local clone. To connect it to a new private GitHub
repository, install GitHub CLI, then run these commands in PowerShell from the
project root:

```powershell
winget install --id GitHub.cli
gh auth login
gh repo create devcompass --private --source=. --remote=origin
git config core.hooksPath .githooks
git config push.autoSetupRemote true
git add .gitignore .github .githooks README.md render.yaml frontend backend other
git status
git commit -m "Initial DevCompass application"
```

Review `git status` before committing. Do not add `.env`, `.venv`, uploaded
documents, Chroma data, `frontend/node_modules`, `frontend/dist`, or unrelated
files. The project `.gitignore` excludes secrets and generated/runtime data.
After the first commit, each commit automatically runs `git push`; GitHub Actions
then runs the frontend build and backend tests.
