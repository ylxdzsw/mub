"""Small local client protocol, serviced by the same owner as the TUI."""

import json
import os
from pathlib import Path
import queue
import socket
import socketserver
import struct
import threading


class ControlServer:
    def __init__(self, root):
        self.path = Path(root) / ".mu" / "mub.sock"
        if len(os.fsencode(self.path)) > 100:
            raise ValueError("Project path is too long for a Unix control socket")
        # The owner lock has already been acquired; any old socket is stale.
        if self.path.exists():
            self.path.unlink()
        self.requests = queue.Queue()
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(30)
                try:
                    line = self.rfile.readline(2_000_001)
                    if len(line) > 2_000_000:
                        raise ValueError("Request is too large")
                    request = json.loads(line)
                    request["_peer_pid"] = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
                    event = threading.Event()
                    result = {}
                    lock = threading.Lock()
                    owner.requests.put((request, event, result, lock))
                    if not event.wait(30):
                        with lock:
                            if not event.is_set():
                                result.update(ok=False, error="Board is not responding; request was not applied")
                                event.set()
                    response = result
                except Exception as error:
                    response = dict(ok=False, error=str(error))
                try:
                    self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def drain(self, handle):
        # Control traffic must not starve process supervision.
        for _ in range(64):
            try:
                request, event, result, lock = self.requests.get_nowait()
            except queue.Empty:
                break
            with lock:
                if event.is_set():
                    continue
                try:
                    # Freeze the response before the owner changes its state again.
                    response = json.loads(json.dumps(handle(request)))
                    result.update(ok=True, result=response)
                except Exception as error:
                    result.update(ok=False, error=str(error))
                event.set()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.path.unlink(missing_ok=True)


def call(root, request):
    path = Path(root) / ".mu" / "mub.sock"
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(35)
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            raise RuntimeError("No running mub for this project. Open the TUI first.") from None
        client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        response = json.loads(client.makefile("rb").readline())
    if not response["ok"]:
        raise ValueError(response["error"])
    return response["result"]
