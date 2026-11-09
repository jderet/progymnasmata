#!/usr/bin/env python3
"""Serveur local de Progymnasmata.

Sert l'application et enregistre toutes les données (images, séances,
enregistrements, réglages) dans le dossier ./data, à côté de l'application.

    python3 server.py            # http://localhost:8000
    python3 server.py 8080       # autre port
"""
import json
import os
import shutil
import sys
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
STORES = {"images", "cards", "sessions", "audio", "kv"}
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
BLOCKED_PREFIXES = ("/data", "/.git", "/.claude")


def store_dir(store):
    d = os.path.join(DATA_DIR, store)
    os.makedirs(d, exist_ok=True)
    return d


def fname(rec_id):
    # Les identifiants peuvent contenir « | » (cartes) : on les encode.
    return quote(rec_id, safe="")


def atomic_write(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    # ---------- utilitaires ----------
    def host_ok(self):
        # Refuse les autres noms d'hôte (protection contre le « DNS rebinding »).
        h = self.headers.get("Host") or ""
        host = h.split("]")[0] + "]" if h.startswith("[") else h.split(":")[0]
        if host not in ALLOWED_HOSTS:
            self.send_error(403, "Host non autorisé")
            return False
        return True

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def api_parts(self):
        """/api/<kind>/<store>[/<id>] -> (kind, store, id) ou None."""
        parts = urlsplit(self.path).path.split("/")[2:]
        if not parts:
            return None
        kind = parts[0]
        store = parts[1] if len(parts) > 1 else None
        rec_id = unquote(parts[2]) if len(parts) > 2 and parts[2] else None
        if store is not None and store not in STORES:
            return None
        if rec_id is not None and (not rec_id or "/" in rec_id or rec_id in (".", "..")):
            return None
        return kind, store, rec_id

    # ---------- GET ----------
    def do_GET(self):
        if not self.host_ok():
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            return self.api_get()
        if path.startswith(BLOCKED_PREFIXES) or path.endswith(".py"):
            return self.send_error(404)
        return super().do_GET()

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def api_get(self):
        p = self.api_parts()
        if p is None:
            return self.send_error(404)
        kind, store, rec_id = p
        if kind == "ping":
            return self.send_json({"ok": True, "dataDir": DATA_DIR})
        if kind == "store" and store and rec_id is None:
            d = store_dir(store)
            out = []
            for f in sorted(os.listdir(d)):
                if f.endswith(".json") and not f.startswith("."):
                    try:
                        with open(os.path.join(d, f), "r", encoding="utf-8") as fh:
                            out.append(json.load(fh))
                    except (OSError, ValueError) as e:
                        print(f"Lecture impossible : {store}/{f} ({e})", file=sys.stderr)
            return self.send_json(out)
        if kind == "store" and store and rec_id:
            path = os.path.join(store_dir(store), fname(rec_id) + ".json")
            if not os.path.exists(path):
                return self.send_json(None, 404)
            with open(path, "r", encoding="utf-8") as fh:
                return self.send_json(json.load(fh))
        if kind == "blob" and store and rec_id:
            path = os.path.join(store_dir(store), fname(rec_id) + ".bin")
            if not os.path.exists(path):
                return self.send_error(404)
            meta = os.path.join(store_dir(store), fname(rec_id) + ".type")
            ctype = "application/octet-stream"
            if os.path.exists(meta):
                with open(meta, "r", encoding="utf-8") as fh:
                    ctype = fh.read().strip() or ctype
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return
        return self.send_error(404)

    # ---------- PUT ----------
    def do_PUT(self):
        if not self.host_ok():
            return
        p = self.api_parts()
        if p is None or not urlsplit(self.path).path.startswith("/api/"):
            return self.send_error(404)
        kind, store, rec_id = p
        if not (store and rec_id):
            return self.send_error(400)
        d = store_dir(store)
        body = self.read_body()
        if kind == "store":
            try:
                rec = json.loads(body.decode("utf-8"))
            except ValueError:
                return self.send_error(400, "JSON invalide")
            atomic_write(os.path.join(d, fname(rec_id) + ".json"),
                         json.dumps(rec, ensure_ascii=False).encode("utf-8"))
            if not rec.get("__blob"):
                for ext in (".bin", ".type"):
                    try:
                        os.unlink(os.path.join(d, fname(rec_id) + ext))
                    except FileNotFoundError:
                        pass
            return self.send_json({"ok": True})
        if kind == "blob":
            atomic_write(os.path.join(d, fname(rec_id) + ".bin"), body)
            atomic_write(os.path.join(d, fname(rec_id) + ".type"),
                         (self.headers.get("Content-Type") or "application/octet-stream").encode("utf-8"))
            return self.send_json({"ok": True})
        return self.send_error(404)

    # ---------- DELETE ----------
    def do_DELETE(self):
        if not self.host_ok():
            return
        p = self.api_parts()
        if p is None or p[0] != "store" or not p[1]:
            return self.send_error(404)
        _, store, rec_id = p
        d = store_dir(store)
        if rec_id is None:  # vider tout le magasin
            for f in os.listdir(d):
                os.unlink(os.path.join(d, f))
        else:
            for ext in (".json", ".bin", ".type"):
                try:
                    os.unlink(os.path.join(d, fname(rec_id) + ext))
                except FileNotFoundError:
                    pass
        return self.send_json({"ok": True})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.makedirs(DATA_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Progymnasmata : http://localhost:{port}")
    print(f"Données enregistrées dans : {DATA_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()
