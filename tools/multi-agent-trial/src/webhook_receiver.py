#!/usr/bin/env python3
"""webhook_receiver.py — isolated T6 delivery receiver (bundled artifact).

Binds 127.0.0.1:<port>/trial, accepts POST (store message + timestamp + hash),
serves GET read-back. Records a webhook receipt: receipt_id, http_status,
correlation_match, body_hash, receiver_timestamp.

Usage:
  python webhook_receiver.py --port 19095 --out /tmp/trial-webhook-state.json [--timeout 300]
"""
import argparse, hashlib, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Receiver(BaseHTTPRequestHandler):
    store = {}          # receipt_id -> record
    lock = threading.Lock()
    out_path = None
    port = 19095

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/trial":
            self._json(404, {"error": "not found"}); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                msg = json.loads(raw)
            except Exception:
                msg = {"raw": raw.decode(errors="replace")}
            receipt_id = msg.get("correlation_id") or ("recv-" + hashlib.sha256(raw).hexdigest()[:16])
            record = {
                "receipt_id": receipt_id,
                "http_status": 200,
                "correlation_match": bool(msg.get("correlation_id")),
                "body_hash": hashlib.sha256(raw).hexdigest(),
                "receiver_timestamp": __import__("time").strftime("%Y-%m-%dT%H:%M:%S+07:00"),
                "message": msg,
            }
            with Receiver.lock:
                Receiver.store[receipt_id] = record
                if Receiver.out_path:
                    with open(Receiver.out_path, "w") as f:
                        json.dump(Receiver.store, f, ensure_ascii=False, indent=2)
            self._json(200, {"ok": True, "receipt_id": receipt_id})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    def do_GET(self):
        if self.path == "/trial/readback":
            with Receiver.lock:
                self._json(200, {"stored": list(Receiver.store.values())})
            return
        self._json(200, {"ok": True, "stored": len(Receiver.store)})

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=19095)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    Receiver.out_path = a.out
    Receiver.port = a.port
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Receiver)
    print(f"webhook receiver listening on 127.0.0.1:{a.port}/trial (pid {os.getpid()})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
