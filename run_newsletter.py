from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser

from app import app


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def find_free_port(start: int = 5000, end: int = 5010) -> int:
    for port in range(start, end + 1):
        if is_port_free(port):
            return port
    raise RuntimeError("No free port found between 5000 and 5010.")


def resolve_port() -> int:
    configured_port = os.environ.get("PORT", "").strip()
    if configured_port:
        port = int(configured_port)
        if not is_port_free(port):
            raise RuntimeError(f"Port {port} is already in use. Stop the existing process or choose another PORT.")
        return port
    return find_free_port()


def open_browser(port: int) -> None:
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    port = resolve_port()
    if port != 5000:
        print("Port 5000 is already in use, so the app is starting on the next free port.")
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    print(f"Opening newsletter on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
