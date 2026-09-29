"""Fixed-target TCP relay for rootless Docker internal-network QA services.

No protocol inspection or query limits: this is deployment ingress, not an adapter.
"""

import select
import socket
import socketserver
import sys
import threading

HOST, PORT = sys.argv[1], int(sys.argv[2])
slots = threading.BoundedSemaphore(16)


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        if not slots.acquire(blocking=False):
            return
        try:
            with socket.create_connection((HOST, PORT), timeout=3) as upstream:
                self.request.settimeout(15)
                upstream.settimeout(15)
                while True:
                    ready, _, _ = select.select([self.request, upstream], [], [], 15)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(4096)
                        if not data:
                            return
                        target = upstream if source is self.request else self.request
                        target.sendall(data)
        except (OSError, TimeoutError):
            return
        finally:
            slots.release()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(("0.0.0.0", PORT), Relay) as server:
    server.serve_forever()
