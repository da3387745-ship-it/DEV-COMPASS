import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SOURCE_DIRECTORY = PROJECT_ROOT / "frontend"
FRONTEND_DIRECTORY = FRONTEND_SOURCE_DIRECTORY / "dist"
VENV_PYTHON = PROJECT_ROOT / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)
BACKEND_PYTHON = str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable))
NPM_COMMAND = "npm.cmd" if os.name == "nt" else "npm"
BACKEND_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://127.0.0.1:8001/"


def port_in_use(port: int) -> bool:
    """Return whether a local service is listening on the given port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def backend_is_ready() -> bool:
    """Return whether port 8000 serves the DevCompass API."""
    try:
        with urlopen(f"{BACKEND_URL}/openapi.json", timeout=1) as response:
            document = json.load(response)
        api_paths = document.get("paths", {})
        required_paths = {
            "/auth/signup",
            "/auth/login",
            "/dashboard",
            "/chat/history",
        }
        return (
            document.get("info", {}).get("title") == "DevCompass RAG Bridge"
            and required_paths.issubset(api_paths)
            and "/account/data" not in api_paths
        )
    except (OSError, URLError, ValueError):
        return False


def frontend_is_ready() -> bool:
    """Return whether port 8001 serves the DevCompass frontend shell."""
    try:
        with urlopen(FRONTEND_URL, timeout=1) as response:
            page = response.read(4096).decode("utf-8", errors="replace")
        return (
            "<title>DevCompass</title>" in page
            and ("assets/app.js" in page)
        )
    except (OSError, URLError, ValueError):
        return False


def wait_until_ready(readiness_check, process, timeout=45) -> bool:
    """Wait until a service responds or its newly started process exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if readiness_check():
            return True
        if process.poll() is not None:
            return readiness_check()
        time.sleep(0.25)
    return readiness_check()


def start_or_reuse(service_name, port, command, readiness_check, processes):
    """Reuse a healthy service or start and verify a new child process."""
    if readiness_check():
        print(f"{service_name} is already running and ready.")
        return
    if port_in_use(port):
        if service_name == "Backend":
            raise RuntimeError(
                "An outdated or different service is using port 8000. Stop the old DevCompass "
                "backend, then run other\\start_app.py again to load account and dashboard routes."
            )
        raise RuntimeError(
            "An older or unrelated frontend is using port 8001. Stop that service, "
            "then run other\\start_app.py again to serve the React build."
        )

    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        creationflags=creation_flags,
    )
    processes.append((service_name, process))
    if not wait_until_ready(readiness_check, process):
        exit_code = process.poll()
        detail = (
            f"process exited with code {exit_code}"
            if exit_code is not None
            else "readiness timed out"
        )
        raise RuntimeError(
            f"{service_name} did not become ready ({detail}). "
            "Check its console for the startup error."
        )
    print(f"{service_name} started and is ready.")


def stop_started_processes(processes):
    """Stop launcher-owned child processes without touching reused services."""
    for name, process in reversed(processes):
        if process.poll() is not None:
            continue
        print(f"Stopping {name}...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def build_frontend() -> None:
    """Build the React frontend before starting the local static file server."""
    try:
        subprocess.run(
            [NPM_COMMAND, "run", "build"],
            cwd=str(FRONTEND_SOURCE_DIRECTORY),
            check=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "Node.js and npm are required. Install Node.js, then run "
            "npm install --prefix frontend."
        ) from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError("The frontend build failed; check the build output.") from error


backend_cmd = [
    BACKEND_PYTHON,
    "-m",
    "uvicorn",
    "backend.bridge:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8000",
]
frontend_cmd = [
    sys.executable,
    "-m",
    "http.server",
    "8001",
    "--directory",
    str(FRONTEND_DIRECTORY),
]

creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def main() -> int:
    """Start or reuse the local services and manage their launcher-owned lifetimes."""
    processes = []
    try:
        build_frontend()
        start_or_reuse("Backend", 8000, backend_cmd, backend_is_ready, processes)
        start_or_reuse("Frontend", 8001, frontend_cmd, frontend_is_ready, processes)
        webbrowser.open(FRONTEND_URL)
        print("DevCompass is ready")
        print("Backend:", BACKEND_URL)
        print("Frontend:", FRONTEND_URL)

        if processes:
            print("Press Ctrl+C to stop services started by this launcher.")
            while True:
                for name, process in processes:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"{name} exited unexpectedly with code {process.returncode}."
                        )
                time.sleep(1)
        return 0
    except KeyboardInterrupt:
        print("Stopping DevCompass services started by this launcher.")
        return 0
    except RuntimeError as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        return 1
    finally:
        stop_started_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())
