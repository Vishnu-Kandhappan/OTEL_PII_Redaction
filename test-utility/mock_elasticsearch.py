#!/usr/bin/env python3
"""Tiny fake Elasticsearch so you can see what each sink receives (no real cluster needed).
Prints: which API key was used, which index, and each document. Run:  python mock_elasticsearch.py [port]"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Elastic-Product", "Elasticsearch")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send({"name": "mock", "version": {"number": "8.13.0"}, "tagline": "You Know, for Search"})
    do_HEAD = do_GET

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        if self.headers.get("Content-Encoding") == "gzip":
            import gzip; raw = gzip.decompress(raw)
        lines = [l for l in raw.decode().splitlines() if l.strip()]
        items, i = [], 0
        while i < len(lines):
            act = json.loads(lines[i]); verb = next(iter(act)); idx = act[verb].get("_index", "?")
            doc = json.loads(lines[i + 1]) if i + 1 < len(lines) else {}
            print(f"[mock-es] auth={self.headers.get('Authorization')} index={idx} doc={json.dumps(doc)[:400]}", flush=True)
            items.append({verb: {"_index": idx, "status": 201, "result": "created"}}); i += 2
        self._send({"took": 1, "errors": False, "items": items})

    def log_message(self, *a): pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9200
    print(f"[mock-es] listening on :{port}", flush=True)
    HTTPServer(("0.0.0.0", port), H).serve_forever()
