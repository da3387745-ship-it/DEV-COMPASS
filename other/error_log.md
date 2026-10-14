# DevCompass Error Log

Current layout: `frontend/` contains the browser app, `backend/` contains the API and RAG data, and `other/` contains support files.

## 2026-09-23

### 1) Frontend served from the wrong directory
- Symptom: browser opened the wrong page because the static server was started from the project root.
- Root cause: the server did not explicitly serve the frontend directory.
- Fix: serve from the `frontend` folder and open the root URL `http://127.0.0.1:8001/` consistently.

### 2) Port collision on backend startup
- Symptom: uvicorn failed with `WinError 10048` because port 8000 was already in use.
- Root cause: two local server processes attempted to bind to the same port.
- Fix: check for port availability before starting and warn instead of crashing.

### 3) RAG backend asked the OpenRouter/groq providers without checking the actual environment keys
- Symptom: runtime failures occur when no provider key is configured or a provider is unavailable.
- Fix: continue to prefer model providers but ensure the backend returns clear messages and only calls configured providers.

### 4) Missing persisted error summary file
- Root cause: there was no project-level file to record recurring configuration and startup issues.
- Fix: create this file to record the recurring issues and their remediation.

## Verification
- Backend import checked successfully with the project virtual environment.
- Static frontend is served from the correct folder.
- Startup scripts now check port collisions before launching services.
