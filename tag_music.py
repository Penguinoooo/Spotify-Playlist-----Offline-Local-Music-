"""
tag_music.py - hands-off metadata + album art tagger for a yt-dlp music folder.

For every audio file it:
  1. Identifies the song (YouTube metadata via the [videoID] in the filename or a
     previously-written youtube_id tag; falls back to parsing the filename).
  2. Looks the song up on MusicBrainz (then iTunes as a second opinion), scoring
     matches on title, artist and duration.
  3. Only if the match is confident: writes artist, title, album, album artist,
     year, track no., genre (iTunes only) and cover art into the file's tags.
  Anything uncertain is left untouched and listed in tag_report.csv for review.

Dry run by default. Use --apply to write tags.

Needs:  pip install mutagen yt-dlp
"""
import argparse, base64, csv, difflib, json, os, re, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

from mutagen import File as MFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TCON, APIC, TXXX
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

AUDIO_EXT = {".mp3", ".opus", ".m4a", ".flac", ".ogg"}
ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[A-Za-z0-9]+$")
UA = "yt-music-tools/1.0"          # overwritten in main() with --contact if given

# ---------- text helpers -------------------------------------------------------------

NOISE = (r"(official|oficial|audio|video|hq|hd|lyrics?|lyric video|visuali[sz]er|explicit|"
         r"remaster(ed)?(\s*\d{4})?|\d{4}\s*remaster(ed)?|music video|m/v|mv)")
NOISE_TAG = re.compile(rf"^[\s,.+/\-!|]*({NOISE}[\s,.+/\-!|]*)+$", re.I)

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"\b(feat|ft|featuring)\.?\s.*$", "", s.lower())      # drop "feat. X"
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)                           # drop (...) / [...]
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and "))
    return re.sub(r"\s+", " ", s).strip()

def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def strip_noise(title):
    def repl(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return "" if NOISE_TAG.match(inner) else m.group(0)
    t = re.sub(r"\(([^()]*)\)|\[([^\[\]]*)\]", repl, title)
    return re.sub(r"\s{2,}", " ", t).strip(" -|")

def split_artist_title(title):
    """'Artist - Song' -> (artist, song). Returns (None, title) if no separator."""
    t = strip_noise(title)
    for sep in (" - ", " – ", " — ", " | ", ": "):
        if sep in t:
            a, s = t.split(sep, 1)
            return a.strip(), s.strip()
    return None, t

# ---------- network --------------------------------------------------------------------

_last_call = {}
def polite(host, gap):
    wait = gap - (time.time() - _last_call.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()

def http_get(url, gap=1.05, binary=False, tries=3):
    host = urllib.parse.urlparse(url).netloc
    for i in range(tries):
        polite(host, gap)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return data if binary else json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None

# ---------- 1. identify the song ------------------------------------------------------

def yt_info(video_id):
    try:
        import yt_dlp
    except ImportError:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception:
        return None

def identify(path, video_id, info):
    """Return dict: artist, title, album, year, duration, thumb, from_yt."""
    out = {"artist": None, "title": None, "album": None, "year": None,
           "duration": None, "thumb": None}
    if info:
        out["duration"] = info.get("duration")
        out["thumb"] = info.get("thumbnail")
        out["album"] = info.get("album")
        out["year"] = info.get("release_year")
        art = info.get("artist") or info.get("creator")
        trk = info.get("track")
        if art and trk:                                    # official/Topic uploads: best case
            out["artist"] = art.split(",")[0].strip()
            out["title"] = trk
            return out
        title = info.get("title") or ""
        a, s = split_artist_title(title)
        chan = re.sub(r"\s*-\s*Topic$", "", info.get("channel") or info.get("uploader") or "")
        out["artist"] = a or chan or None
        out["title"] = s
        return out
    a, s = split_artist_title(re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", path.stem))
    out["artist"], out["title"] = a, s
    return out

# ---------- 2. look it up -------------------------------------------------------------

def mb_lookup(ident):
    if not ident["title"]:
        return None
    q = f'recording:"{ident["title"]}"'
    if ident["artist"]:
        q += f' AND artist:"{ident["artist"]}"'
    url = ("https://musicbrainz.org/ws/2/recording?fmt=json&limit=15&query="
           + urllib.parse.quote(q))
    data = http_get(url)
    if not data:
        return None
    best, best_score = None, 0.0
    for rec in data.get("recordings", []):
        credit = " ".join(c.get("name", "") for c in rec.get("artist-credit", []))
        ts = sim(ident["title"], rec.get("title"))
        as_ = sim(ident["artist"], credit) if ident["artist"] else 0.5
        score = 0.55 * ts + 0.35 * as_
        if ident["duration"] and rec.get("length"):
            diff = abs(ident["duration"] - rec["length"] / 1000)
            score += 0.10 if diff <= 5 else (0.04 if diff <= 12 else -0.10)
        else:
            score += 0.03
        if score > best_score:
            best, best_score, best_credit = rec, score, credit
    if not best:
        return None
    rel = pick_release(best.get("releases", []))
    res = {"source": "musicbrainz", "score": round(best_score, 3),
           "artist": best_credit, "title": best["title"], "album": None,
           "album_artist": None, "year": None, "track": None, "tracks": None,
           "genre": None, "cover_url": None, "release_id": None, "rg_id": None}
    if rel:
        res["album"] = rel.get("title")
        res["year"] = (rel.get("date") or "")[:4] or None
        res["release_id"] = rel["id"]
        res["rg_id"] = (rel.get("release-group") or {}).get("id")
        full = http_get(f'https://musicbrainz.org/ws/2/release/{rel["id"]}?fmt=json&inc=recordings+artist-credits')
        if full:
            res["album_artist"] = " ".join(c.get("name", "") for c in full.get("artist-credit", [])) or None
            for medium in full.get("media", []):
                for t in medium.get("tracks", []):
                    if (t.get("recording") or {}).get("id") == best["id"]:
                        res["track"], res["tracks"] = t.get("number"), medium.get("track-count")
    return res

def pick_release(releases):
    """Prefer official studio albums, then singles/EPs; avoid compilations/live; earliest date."""
    def rank(r):
        rg = r.get("release-group") or {}
        ptype = rg.get("primary-type") or ""
        sec = rg.get("secondary-types") or []
        r1 = 0 if r.get("status") == "Official" else 1
        r2 = {"Album": 0, "EP": 1, "Single": 2}.get(ptype, 3)
        r3 = 1 if sec else 0
        return (r1, r3, r2, r.get("date") or "9999")
    return sorted(releases, key=rank)[0] if releases else None

def itunes_lookup(ident):
    if not ident["title"]:
        return None
    term = f'{ident["artist"] or ""} {ident["title"]}'.strip()
    data = http_get("https://itunes.apple.com/search?entity=song&limit=10&term="
                    + urllib.parse.quote(term), gap=3.0)
    if not data:
        return None
    best, best_score = None, 0.0
    for r in data.get("results", []):
        ts = sim(ident["title"], r.get("trackName"))
        as_ = sim(ident["artist"], r.get("artistName")) if ident["artist"] else 0.5
        score = 0.55 * ts + 0.35 * as_
        if ident["duration"] and r.get("trackTimeMillis"):
            diff = abs(ident["duration"] - r["trackTimeMillis"] / 1000)
            score += 0.10 if diff <= 5 else (0.04 if diff <= 12 else -0.10)
        else:
            score += 0.03
        if score > best_score:
            best, best_score = r, score
    if not best:
        return None
    art = (best.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
    return {"source": "itunes", "score": round(best_score, 3),
            "artist": best.get("artistName"), "title": best.get("trackName"),
            "album": best.get("collectionName"), "album_artist": best.get("collectionArtistName") or best.get("artistName"),
            "year": (best.get("releaseDate") or "")[:4] or None,
            "track": str(best.get("trackNumber") or "") or None,
            "tracks": best.get("trackCount"), "genre": best.get("primaryGenreName"),
            "cover_url": art or None, "release_id": None, "rg_id": None}

def find_cover(match, ident, allow_thumb):
    urls = []
    if match.get("release_id"):
        urls.append(f'https://coverartarchive.org/release/{match["release_id"]}/front-500')
    if match.get("rg_id"):
        urls.append(f'https://coverartarchive.org/release-group/{match["rg_id"]}/front-500')
    if match.get("cover_url"):
        urls.append(match["cover_url"])
    if allow_thumb and ident.get("thumb"):
        urls.append(ident["thumb"])
    for u in urls:
        img = http_get(u, gap=0.3, binary=True)
        if img and len(img) > 2000:
            return img
    return None

# ---------- 3. write tags -------------------------------------------------------------

def mime_of(img):
    return "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"

def read_youtube_id(path):
    try:
        f = MFile(path)
        if f is None or f.tags is None:
            return None
        if isinstance(f.tags, ID3) or path.suffix.lower() == ".mp3":
            fr = ID3(path).getall("TXXX:youtube_id")
            return str(fr[0].text[0]) if fr else None
        if isinstance(f, MP4):
            v = f.tags.get("----:com.apple.iTunes:youtube_id")
            return v[0].decode() if v else None
        v = f.tags.get("youtube_id")
        return v[0] if v else None
    except Exception:
        return None

def write_tags(path, m, img, video_id):
    ext = path.suffix.lower()
    year, track = m.get("year"), m.get("track")
    trk = f'{track}/{m["tracks"]}' if track and m.get("tracks") else (track or "")
    if ext == ".mp3":
        try:
            t = ID3(path)
        except ID3NoHeaderError:
            t = ID3()
        for k in ("TIT2", "TPE1", "TPE2", "TALB", "TDRC", "TRCK", "TCON", "APIC", "TXXX:youtube_id"):
            t.delall(k)
        t.add(TIT2(encoding=3, text=m["title"])); t.add(TPE1(encoding=3, text=m["artist"]))
        if m.get("album_artist"): t.add(TPE2(encoding=3, text=m["album_artist"]))
        if m.get("album"): t.add(TALB(encoding=3, text=m["album"]))
        if year: t.add(TDRC(encoding=3, text=str(year)))
        if trk: t.add(TRCK(encoding=3, text=trk))
        if m.get("genre"): t.add(TCON(encoding=3, text=m["genre"]))
        if img: t.add(APIC(encoding=3, mime=mime_of(img), type=3, desc="Cover", data=img))
        if video_id: t.add(TXXX(encoding=3, desc="youtube_id", text=video_id))
        t.save(path, v2_version=3)
    elif ext == ".m4a":
        f = MP4(path)
        if f.tags is None: f.add_tags()
        f["\xa9nam"], f["\xa9ART"] = [m["title"]], [m["artist"]]
        if m.get("album_artist"): f["aART"] = [m["album_artist"]]
        if m.get("album"): f["\xa9alb"] = [m["album"]]
        if year: f["\xa9day"] = [str(year)]
        if track and str(track).isdigit():
            f["trkn"] = [(int(track), int(m["tracks"] or 0))]
        if m.get("genre"): f["\xa9gen"] = [m["genre"]]
        if img:
            fmt = MP4Cover.FORMAT_PNG if img[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG
            f["covr"] = [MP4Cover(img, imageformat=fmt)]
        if video_id:
            f["----:com.apple.iTunes:youtube_id"] = [MP4FreeForm(video_id.encode())]
        f.save()
    else:                                              # opus / ogg / flac (Vorbis comments)
        f = {".opus": OggOpus, ".ogg": OggVorbis, ".flac": FLAC}[ext](path)
        if f.tags is None: f.add_tags()
        f["title"], f["artist"] = [m["title"]], [m["artist"]]
        if m.get("album_artist"): f["albumartist"] = [m["album_artist"]]
        if m.get("album"): f["album"] = [m["album"]]
        if year: f["date"] = [str(year)]
        if track: f["tracknumber"] = [str(track)]
        if m.get("tracks"): f["tracktotal"] = [str(m["tracks"])]
        if m.get("genre"): f["genre"] = [m["genre"]]
        if video_id: f["youtube_id"] = [video_id]
        if img:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, mime_of(img), "Cover", img
            if ext == ".flac":
                f.clear_pictures(); f.add_picture(pic)
            else:
                f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()

# ---------- main ----------------------------------------------------------------------

def main():
    global UA
    ap = argparse.ArgumentParser(description="Tag a music folder with metadata + album art.")
    ap.add_argument("folder")
    ap.add_argument("--apply", action="store_true", help="write tags (default: dry run)")
    ap.add_argument("--min-score", type=float, default=0.85, help="auto-accept threshold (0-1)")
    ap.add_argument("--contact", default=os.environ.get("MB_CONTACT", ""),
                    help="your email/URL, sent in the User-Agent (MusicBrainz asks for this)")
    ap.add_argument("--no-thumb", action="store_true", help="never use the YouTube thumbnail as cover")
    ap.add_argument("--force", action="store_true", help="ignore the cache and re-look everything up")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N files (for testing)")
    a = ap.parse_args()
    if a.contact:
        UA = f"yt-music-tools/1.0 ( {a.contact} )"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    folder = Path(a.folder)
    cache_path = folder / "tag_cache.json"
    cache = {} if a.force or not cache_path.exists() else json.loads(cache_path.read_text(encoding="utf-8"))
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    if a.limit:
        files = files[:a.limit]

    report, tagged, review = [], 0, 0
    for i, p in enumerate(files, 1):
        m_id = ID_RE.search(p.name)
        vid = (m_id.group(1) if m_id else None) or read_youtube_id(p)
        key = vid or p.name
        rec = cache.get(key)
        if rec is None:
            info = yt_info(vid) if vid else None
            ident = identify(p, vid, info)
            best = None
            for fn in (mb_lookup, itunes_lookup):
                res = fn(ident)
                if res and (best is None or res["score"] > best["score"]):
                    best = res
                if best and best["score"] >= a.min_score:
                    break
            rec = {"ident": {k: v for k, v in ident.items()}, "match": best}
            cache[key] = rec
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        best, ident = rec["match"], rec["ident"]
        ok = bool(best and best["score"] >= a.min_score)
        status = "matched" if ok else "review"
        if ok and a.apply:
            img = find_cover(best, ident, not a.no_thumb)
            try:
                write_tags(p, best, img, vid)
                status = "tagged"
            except Exception as e:
                status = f"error: {e}"
        tagged += status == "tagged"
        review += status == "review"
        if best:
            detail = (f"{best.get('artist')} - {best.get('title')} | {best.get('album')} "
                      f"({best.get('year')}) [{best.get('source')}]")
        else:
            detail = f"no match (guessed: {ident.get('artist')} - {ident.get('title')})"
        print(f"[{i}/{len(files)}] {status:8} {best['score'] if best else 0:<5} {p.name}\n           -> {detail}")
        report.append([p.name, status, best["score"] if best else "", (best or {}).get("source", ""),
                       (best or {}).get("artist", ""), (best or {}).get("title", ""),
                       (best or {}).get("album", ""), (best or {}).get("year", ""),
                       ident.get("artist") or "", ident.get("title") or ""])

    with open(folder / "tag_report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["file", "status", "score", "source", "artist", "title", "album", "year",
                    "guessed_artist", "guessed_title"])
        w.writerows(report)
    print(f"\n{len(files)} files | {'tagged' if a.apply else 'would tag'}: "
          f"{sum(r[1] in ('tagged','matched') for r in report)} | needs review: {review}")
    print("Report: tag_report.csv" + ("" if a.apply else "   (dry run - re-run with --apply to write tags)"))

if __name__ == "__main__":
    main()
