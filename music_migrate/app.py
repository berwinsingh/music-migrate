"""The app: a local web page served from this machine (only it can read the Music app or hold your logins)."""

import io
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import (DESTS, SOURCES, MigrateError, choose, connect_spotify, connect_ytmusic, connected,
               disconnect, match, organize, parse_csv, tkey, transfer, write_csv)

PAGE = Path(__file__).with_name("index.html")

# One person, one migration at a time: plain module state is enough.
job = {"phase": "idle", "done": 0, "total": 0, "message": "", "error": None, "results": []}
work = {}  # source, dest name, dest, lib, playlists, matches, ids


def run(phase, fn):
    """Run fn in the background, reporting progress into `job`."""
    if job["phase"] in ("scanning", "transferring"):
        raise MigrateError("Already working, hang on.")
    job.update(phase=phase, done=0, total=0, message="Starting…", error=None)

    def progress(i, n, msg=""):
        job.update(done=i, total=n, message=msg)

    def target():
        try:
            job["phase"] = fn(progress)
        except Exception as e:  # shown in the app, not swallowed
            job.update(phase="error", error=str(e))

    threading.Thread(target=target, daemon=True).start()


def scan(body):
    source, dest = body["source"], body["dest"]

    def go(progress):
        progress(0, 0, "If your Mac asks to let this app control Music, click OK." if source == "apple" else "")
        lib = parse_csv(io.StringIO(body["csv"])) if source == "csv" else SOURCES[source](None)
        work.update(source=source, dest_name=dest, lib=lib, matches={}, ids={})
        if dest == "csv":
            return "export"
        d = DESTS[dest](dry_run=True)  # matching works before sign-in where the service allows it
        playlists = {**lib, **organize(lib, int(body.get("min_genre") or 0))}
        matches = match(d, dest, playlists, progress)
        work.update(dest=d, playlists=playlists, matches=matches, ids=choose(matches))
        return "review"

    run("scanning", go)


def tracks():
    dest = work.get("dest")
    url, art = getattr(dest, "url", "{}"), getattr(dest, "art", "")
    in_lists = {}
    for name, ts in work.get("playlists", {}).items():
        for t in ts:
            in_lists.setdefault(tkey(t), []).append(name)
    return [{"key": k, "title": t["title"], "artist": t["artist"], "album": t.get("album", ""),
             "duration": t.get("duration", 0), "playlists": in_lists.get(k, []), "choice": work["ids"].get(k),
             "candidates": [{**c, "score": s, "url": url.format(c["id"]), "art": c.get("art") or art.format(c["id"])}
                            for s, c in ranked[:5]]}
            for k, (t, ranked) in work.get("matches", {}).items()]


def start_transfer(body):
    def go(progress):
        dest = DESTS[work["dest_name"]]()  # signed in now
        job["results"] = transfer(dest, work["source"], work["lib"], work["playlists"], work["ids"],
                                  like=body.get("like", True), library_playlist=body.get("library_playlist", False),
                                  progress=progress)
        return "done"

    run("transferring", go)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, body, status=200, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        # Only this app's own page: blocks other websites and DNS-rebinding from poking the local server.
        host_ok = self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
        return host_ok and (self.command == "GET" or self.headers.get("X-Music-Migrate") == "1")

    def do_GET(self):
        if not self.trusted():
            return self.send({"error": "forbidden"}, 403)
        if self.path == "/":
            return self.send(PAGE.read_bytes(), kind="text/html; charset=utf-8")
        if self.path == "/api/status":
            return self.send({**job, "connected": connected(), "source": work.get("source"), "dest": work.get("dest_name")})
        if self.path == "/api/tracks":
            return self.send(tracks())
        if self.path == "/api/export.csv":
            f = io.StringIO()
            write_csv(work.get("lib", {}), f)
            return self.send(f.getvalue().encode(), kind="text/csv; charset=utf-8")
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        if not self.trusted():
            return self.send({"error": "forbidden"}, 403)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            if self.path == "/api/connect/ytmusic":
                connect_ytmusic(body["headers"])
            elif self.path == "/api/connect/spotify":
                connect_spotify(body["client_id"], body["client_secret"])
            elif self.path == "/api/disconnect":
                disconnect(body["service"])
            elif self.path == "/api/scan":
                scan(body)
            elif self.path == "/api/choice":
                work["ids"][body["key"]] = body["id"]
            elif self.path == "/api/transfer":
                start_transfer(body)
            elif self.path == "/api/reset":
                job.update(phase="idle", error=None, results=[])
            else:
                return self.send({"error": "not found"}, 404)
        except MigrateError as e:
            return self.send({"error": str(e)}, 400)
        self.send({"ok": True})


def serve(port=0, open_browser=True):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Music Migrate is open at {url}  (Ctrl-C to quit)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
