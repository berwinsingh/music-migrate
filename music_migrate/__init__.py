"""Move your music library between Apple Music, Spotify, YouTube Music and CSV.

    music-migrate --from apple --to ytmusic --dry-run     # match only, write report
    music-migrate --from apple --to ytmusic               # like songs + build playlists

A library is {"Library": [tracks], "<playlist name>": [tracks], ...}.
A track is a dict: title, artist, album, genre, duration (seconds), fav.
"""

import argparse
import csv
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

LIBRARY = "Library"
THRESHOLD = 0.75  # below this a match is reported but not added
APP_DIR = Path.home() / ".music-migrate"  # logins and the search cache live here, never in the repo
CACHE = APP_DIR / "search-cache.json"
YTM_AUTH = APP_DIR / "ytmusic-auth.json"
SPOTIFY_KEYS = APP_DIR / "spotify-app.json"


class MigrateError(Exception):
    """A problem the user can fix; the CLI prints it, the app shows it."""


def tkey(t):
    return f"{t['title']}\n{t['artist']}"


# ---------- matching ----------

def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))  # señorita == senorita
    s = re.sub(r"[(\[](from|feat|ft|with|prod)\b[^)\]]*[)\]]", " ", s)
    s = re.sub(r"\s(feat|ft)\.?\s.*", " ", s)
    s = re.sub(r"\s-\s.*(remaster|version|edit|mono|stereo).*", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)  # \w is unicode, keeps non-latin titles
    return " ".join(s.split())


def artists(s):
    return [norm(a) for a in re.split(r",|&|/|\||\bx\b|\band\b|\bfeat\.?|\bft\.?", (s or "").lower()) if norm(a)]


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def score(track, cand):
    """0..1 similarity between a source track and a search result."""
    if not track.get("artist") and " - " in track["title"]:  # "Alan Walker - Dreamer" style file names
        a, ti = track["title"].split(" - ", 1)
        track = {**track, "artist": a, "title": ti}
    def keys(s):  # full title and title without (...)/[...], spaces dropped: "4mendown" == "4 Men Down"
        return {norm(s).replace(" ", ""), norm(re.sub(r"[(\[].*?[)\]]", "", s or "")).replace(" ", "")} - {""}
    t, c = keys(track["title"]), keys(cand["title"])
    # squared: "Jeene Ki Tamanna" vs "Panna Ki Tamanna" is a different song, not a typo
    title = 1.0 if t & c else max((ratio(a, b) ** 2 for a in t for b in c), default=0)
    src, dst = artists(track["artist"]), artists(cand["artist"])
    artist = max((ratio(a, b) for a in src for b in dst), default=0.8)  # unknown artist: mild penalty
    s = title * (0.5 + 0.5 * artist)  # right title by the wrong artist is a cover, not a match
    if track.get("duration") and cand.get("duration") and abs(track["duration"] - cand["duration"]) > 30:
        s -= 0.15
    return round(s, 3)


def best(track, cands):
    scored = [(score(track, c), c) for c in cands]
    return max(scored, key=lambda x: x[0], default=(0, None))


# ---------- sources ----------

APPLE_JXA = r"""
const m = Application("Music");
function dump(ts) {
  const n = ts.name(), a = ts.artist(), al = ts.album(), g = ts.genre(), d = ts.duration();
  let f; try { f = ts.favorited() } catch (e) { try { f = ts.loved() } catch (e2) { f = [] } }
  return n.map((x, i) => ({title: x, artist: a[i], album: al[i], genre: g[i], duration: Math.round(d[i]), fav: !!f[i]}));
}
const out = {Library: dump(m.libraryPlaylists[0].tracks)};
for (const p of m.userPlaylists()) {
  if (p.smart() || p.specialKind() !== "none" || p.tracks.length === 0) continue;
  out[p.name()] = dump(p.tracks);
}
JSON.stringify(out);
"""


def read_apple(args):
    if sys.platform != "darwin":
        raise MigrateError("Reading Apple Music needs the Music app on a Mac. Elsewhere, export a CSV instead.")
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", APPLE_JXA], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise MigrateError("The Music app didn't answer. If your Mac asked whether this app may control Music, "
                           "click OK (or allow it in System Settings → Privacy & Security → Automation) and try again.")
    if out.returncode:
        raise MigrateError(f"Could not read the Music app: {out.stderr.strip()}")
    return json.loads(out.stdout)


def read_csv(args):
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        return parse_csv(f)


def parse_csv(f):
    """Any CSV with title/artist columns (also accepts Exportify's 'Track Name'/'Artist Name(s)')."""
    lib = {}
    for r in csv.DictReader(f):
        title = r.get("title") or r.get("Track Name")
        if not title:
            continue
        lib.setdefault(r.get("playlist") or LIBRARY, []).append({
            "title": title,
            "artist": r.get("artist") or r.get("Artist Name(s)", ""),
            "album": r.get("album") or r.get("Album Name", ""),
            "genre": r.get("genre", ""),
            "duration": int(float(r.get("duration") or 0)) or int(r.get("Duration (ms)") or 0) // 1000,
            "fav": r.get("fav", "").lower() in ("1", "true", "yes"),
        })
    return lib


def ytm_track(x):
    return {
        "id": x.get("videoId"),
        "title": x.get("title", ""),
        "artist": ", ".join(a["name"] for a in x.get("artists") or []),
        "album": (x.get("album") or {}).get("name", ""),
        "genre": "",
        "duration": x.get("duration_seconds") or 0,
        "fav": False,
    }


def read_ytmusic():
    yt = ytmusic(required=True)
    lib = {LIBRARY: [ytm_track(x) for x in yt.get_liked_songs(limit=None)["tracks"]]}
    for p in yt.get_library_playlists(limit=None):
        if p["playlistId"] in ("LM", "SE"):  # Liked Music, Episodes
            continue
        lib[p["title"]] = [ytm_track(x) for x in yt.get_playlist(p["playlistId"], limit=None)["tracks"]]
    return lib


def sp_track(x):
    return {
        "id": x["id"],
        "title": x["name"],
        "artist": ", ".join(a["name"] for a in x["artists"]),
        "album": x["album"]["name"],
        "genre": "",
        "duration": x["duration_ms"] // 1000,
        "art": (x["album"].get("images") or [{}])[-1].get("url", ""),
        "fav": False,
    }


def sp_all(sp, page):
    while page:
        yield from page["items"]
        page = sp.next(page)


def read_spotify():
    sp = spotify()
    lib = {LIBRARY: [sp_track(i["track"]) for i in sp_all(sp, sp.current_user_saved_tracks(limit=50)) if i.get("track")]}
    for p in sp_all(sp, sp.current_user_playlists(limit=50)):
        items = sp_all(sp, sp.playlist_items(p["id"], limit=100))
        lib[p["name"]] = [sp_track(i["track"]) for i in items if i.get("track") and i["track"].get("id")]
    return lib


# ---------- destinations ----------

def ytmusic(required=False):
    from ytmusicapi import YTMusic, OAuthCredentials
    if not YTM_AUTH.exists():
        if required:
            raise MigrateError("Sign in to YouTube Music first.")
        return YTMusic()  # anonymous: search works, writing doesn't
    creds = None
    if os.environ.get("YTMUSIC_CLIENT_ID"):
        creds = OAuthCredentials(os.environ["YTMUSIC_CLIENT_ID"], os.environ["YTMUSIC_CLIENT_SECRET"])
    return YTMusic(str(YTM_AUTH), oauth_credentials=creds)


def ytm_login(text):
    """Pull the login out of whatever was pasted: 'Copy as cURL' (bash or Windows cmd), request headers, or a bare cookie."""
    if '^"' in text:  # Windows "Copy as cURL (cmd)" escapes with ^
        text = text.replace("^\n", "\n").replace("^", "")
    found = {}
    try:
        tokens = shlex.split(text.replace("\\\n", " "))
    except ValueError:
        tokens = []
    if "curl" in tokens[:1]:
        for flag, value in zip(tokens, tokens[1:]):
            if flag in ("-H", "--header") and ":" in value:
                name, _, value = value.partition(":")
                found[name.strip().lower()] = value.strip()
            elif flag in ("-b", "--cookie"):
                found["cookie"] = value
    else:  # headers as "name: value", or Chrome's name-then-value lines
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            name, sep, value = line.partition(":")
            if sep and name and value.strip():
                found.setdefault(name.strip().lower(), value.strip())
            elif i + 1 < len(lines):
                found.setdefault(line.rstrip(":").lower(), lines[i + 1])
    cookie = found.get("cookie") or (text.strip() if "=" in text and "\n" not in text.strip() else "")
    if "__Secure-3PAPISID=" not in cookie:
        raise MigrateError("That doesn't include your YouTube Music login. Right-click a 'browse' request and choose Copy → Copy as cURL, "
                           "and make sure you're signed in on music.youtube.com.")
    # ytmusicapi re-signs every request from the cookie; the authorization value only marks this as a browser login.
    return f"cookie: {cookie}\nx-goog-authuser: {found.get('x-goog-authuser', '0')}\nauthorization: SAPISIDHASH 0"


def connect_ytmusic(headers):
    """Save a login copied out of the browser, and prove it works."""
    from ytmusicapi import setup
    login = ytm_login(headers)
    APP_DIR.mkdir(mode=0o700, exist_ok=True); APP_DIR.chmod(0o700)  # logins live here: owner only
    try:
        setup(filepath=str(YTM_AUTH), headers_raw=login)
        ytmusic(required=True).get_library_playlists(limit=1)
    except Exception as e:
        YTM_AUTH.unlink(missing_ok=True)
        raise MigrateError(f"YouTube Music didn't accept that login ({e}). Reload music.youtube.com and copy a fresh 'browse' request.") from e


class YTMusicDest:
    url = "https://music.youtube.com/watch?v={}"
    art = "https://i.ytimg.com/vi/{}/mqdefault.jpg"  # square album art sits centered in this frame

    def __init__(self, dry_run=False):
        self.yt = ytmusic(required=not dry_run)

    def search(self, t):
        q = f"{t['title']} {artists(t['artist'])[0] if artists(t['artist']) else ''}"
        return [ytm_track(x) for x in self.yt.search(q, filter="songs", limit=5) if x.get("videoId")]

    def like(self, ids, progress):
        from ytmusicapi.models.content.enums import LikeStatus
        for i, vid in enumerate(ids, 1):
            self.yt.rate_song(vid, LikeStatus.LIKE)
            progress(i, len(ids))
            time.sleep(0.2)  # ponytail: fixed pacing, back off on 429s if YouTube starts refusing

    def playlist(self, name, ids):
        existing = {p["title"]: p["playlistId"] for p in self.yt.get_library_playlists(limit=None)}
        pid = existing.get(name) or self.yt.create_playlist(name, "Imported with music-migrate")
        for i in range(0, len(ids), 100):
            self.yt.add_playlist_items(pid, ids[i:i + 100], duplicates=False)
        return f"https://music.youtube.com/playlist?list={pid}"


def spotify():
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    keys = json.loads(SPOTIFY_KEYS.read_text()) if SPOTIFY_KEYS.exists() else {}
    cid = os.environ.get("SPOTIPY_CLIENT_ID") or keys.get("client_id")
    secret = os.environ.get("SPOTIPY_CLIENT_SECRET") or keys.get("client_secret")
    if not (cid and secret):
        raise MigrateError("Connect Spotify first (it needs your Spotify app's Client ID and Secret).")
    scope = "user-library-read user-library-modify playlist-read-private playlist-modify-private playlist-modify-public"
    APP_DIR.mkdir(mode=0o700, exist_ok=True); APP_DIR.chmod(0o700)  # logins live here: owner only
    auth = SpotifyOAuth(cid, secret, os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
                        scope=scope, cache_path=str(APP_DIR / "spotify-token.json"))
    return spotipy.Spotify(auth_manager=auth)


def connect_spotify(client_id, client_secret):
    """Save the app keys, then sign in (spotipy opens the browser for approval)."""
    APP_DIR.mkdir(mode=0o700, exist_ok=True); APP_DIR.chmod(0o700)  # logins live here: owner only
    SPOTIFY_KEYS.write_text(json.dumps({"client_id": client_id.strip(), "client_secret": client_secret.strip()}))
    try:
        spotify().me()
    except Exception as e:
        SPOTIFY_KEYS.unlink(missing_ok=True)
        raise MigrateError(f"Spotify sign-in failed ({e}).") from e


def disconnect(service):
    files = {"ytmusic": [YTM_AUTH], "spotify": [SPOTIFY_KEYS, APP_DIR / "spotify-token.json"]}[service]
    for f in files:
        f.unlink(missing_ok=True)


def connected():
    return {"apple": sys.platform == "darwin", "ytmusic": YTM_AUTH.exists(),
            "spotify": SPOTIFY_KEYS.exists() or bool(os.environ.get("SPOTIPY_CLIENT_ID")), "csv": True}


class SpotifyDest:
    url = "https://open.spotify.com/track/{}"

    def __init__(self, dry_run=False):
        self.sp = spotify()

    def search(self, t):
        q = f"track:{norm(t['title'])} artist:{artists(t['artist'])[0] if artists(t['artist']) else ''}"
        items = self.sp.search(q, type="track", limit=5)["tracks"]["items"]
        if not items:
            items = self.sp.search(f"{t['title']} {t['artist']}", type="track", limit=5)["tracks"]["items"]
        return [sp_track(x) for x in items]

    def like(self, ids, progress):
        for i in range(0, len(ids), 50):
            self.sp.current_user_saved_tracks_add(ids[i:i + 50])
            progress(min(i + 50, len(ids)), len(ids))

    def playlist(self, name, ids):
        p = self.sp.current_user_playlist_create(name, public=False, description="Imported with music-migrate")
        for i in range(0, len(ids), 100):
            self.sp.playlist_add_items(p["id"], ids[i:i + 100])
        return p["external_urls"]["spotify"]


def write_csv(lib, f):
    w = csv.DictWriter(f, ["playlist", "title", "artist", "album", "genre", "duration", "fav"], extrasaction="ignore")
    w.writeheader()
    for name, tracks in lib.items():
        for t in tracks:
            w.writerow({"playlist": name, **t})


# ---------- migrate ----------

def organize(lib, min_genre):
    """Extra playlists built from the library: favorites and big genres."""
    tracks = lib.get(LIBRARY, [])
    extra = {}
    favs = [t for t in tracks if t.get("fav")]
    if favs:
        extra["Favorites"] = favs
    if min_genre:
        genres = {}
        for t in tracks:
            if t.get("genre"):
                genres.setdefault(t["genre"], []).append(t)
        extra.update({g: ts for g, ts in sorted(genres.items()) if len(ts) >= min_genre})
    return extra


def noop(*_):
    pass


def match(dest, dest_name, playlists, progress=noop):
    """{tkey: (track, [(score, candidate), ...] best first)} for every unique track."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    unique = {tkey(t): t for ts in playlists.values() for t in ts}
    out = {}
    for n, (key, t) in enumerate(unique.items(), 1):
        ck = f"{dest_name}|{key}"
        if ck not in cache:  # cache raw results so re-runs resume and re-score without searching again
            cache[ck] = dest.search(t)
            APP_DIR.mkdir(mode=0o700, exist_ok=True); APP_DIR.chmod(0o700)  # logins live here: owner only
            CACHE.write_text(json.dumps(cache))
        out[key] = (t, sorted(((score(t, c), c) for c in cache[ck]), key=lambda x: -x[0]))
        progress(n, len(unique), t["title"])
    return out


def choose(matches, threshold=THRESHOLD):
    """Default pick per track: the best candidate if it clears the threshold, else nothing."""
    return {k: r[0][1]["id"] if r and r[0][0] >= threshold else None for k, (_, r) in matches.items()}


def transfer(dest, source, lib, playlists, ids, like=True, library_playlist=False, progress=noop):
    """Like the library and build playlists from the chosen ids. Returns [(playlist title, url)]."""
    def resolved(ts):
        return list(dict.fromkeys(ids[tkey(t)] for t in ts if ids.get(tkey(t))))

    if like:
        dest.like(resolved(lib.get(LIBRARY, [])), lambda i, n: progress(i, n, "Liking songs"))
    done = []
    names = [n for n in playlists if n != LIBRARY or library_playlist]
    for i, name in enumerate(names, 1):
        title = f"{source.title()} {name}" if name == LIBRARY else name
        progress(i, len(names), f"Playlist: {title}")
        done.append((title, dest.playlist(title, resolved(playlists[name]))))
    return done


def migrate(args):
    lib = SOURCES[args.source](args)
    print(f"Read {sum(map(len, lib.values()))} tracks from {args.source}: "
          + ", ".join(f"{k} ({len(v)})" for k, v in lib.items()))
    if args.dest == "csv":
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            write_csv(lib, f)
        return print(f"Wrote {args.out}")

    def bar(i, n, msg=""):
        print(f"\r  {i}/{n} {msg[:50]:50}", end="" if i < n else "\n", flush=True)

    dest = DESTS[args.dest](args.dry_run)
    playlists = {**lib, **organize(lib, args.min_genre)}
    matches = match(dest, args.dest, playlists, bar)
    ids = choose(matches, args.threshold)

    report = []
    for key, (t, ranked) in matches.items():
        s, m = ranked[0] if ranked else (0, {})
        report.append({"title": t["title"], "artist": t["artist"], "match_title": m.get("title", ""),
                       "match_artist": m.get("artist", ""), "score": s, "id": m.get("id", ""),
                       "status": "matched" if ids[key] else ("low-confidence" if m else "not-found")})
    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(report[0]))
        w.writeheader()
        w.writerows(sorted(report, key=lambda r: r["score"]))
    print(f"{sum(map(bool, ids.values()))}/{len(ids)} matched. Review the weakest matches at the top of {args.report}.")
    if args.dry_run:
        return
    for title, url in transfer(dest, args.source, lib, playlists, ids, args.like, args.library_playlist, bar):
        print(f"{title}: {url}")


SOURCES = {"apple": read_apple, "csv": read_csv, "ytmusic": lambda a: read_ytmusic(), "spotify": lambda a: read_spotify()}
DESTS = {"ytmusic": YTMusicDest, "spotify": SpotifyDest}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:  # no arguments: open the app
        from .app import serve
        return serve()
    p = argparse.ArgumentParser(prog="music-migrate", description=(__doc__ or "").split("\n")[0],
                                epilog="Run with no arguments to open the app instead.")
    p.add_argument("--from", dest="source", choices=SOURCES, required=True)
    p.add_argument("--to", dest="dest", choices=[*DESTS, "csv"], required=True)
    p.add_argument("--csv", help="input CSV for --from csv")
    p.add_argument("--out", default="library.csv", help="output for --to csv")
    p.add_argument("--report", default="migration-report.csv")
    p.add_argument("--dry-run", action="store_true", help="match and report only, change nothing")
    p.add_argument("--no-like", dest="like", action="store_false", help="don't like/save library songs")
    p.add_argument("--library-playlist", action="store_true", help="also put the whole library in one playlist")
    p.add_argument("--min-genre", type=int, default=10, help="genre playlists for genres with at least N songs (0 = off)")
    p.add_argument("--threshold", type=float, default=THRESHOLD)
    args = p.parse_args(argv)
    if args.source == "csv" and not args.csv:
        p.error("--from csv needs --csv FILE")
    try:
        migrate(args)
    except MigrateError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
