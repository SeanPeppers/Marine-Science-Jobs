import threading
import webbrowser

HOST = "127.0.0.1"  # localhost only: the app reads resumes and writes API keys
PORT = 8000


def main() -> None:
    import uvicorn

    url = f"http://{HOST}:{PORT}"
    threading.Timer(1.5, webbrowser.open, args=[url]).start()
    uvicorn.run("marine_jobs.app:app", host=HOST, port=PORT)
