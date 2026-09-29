import csv, os, sys
from yt_dlp import YoutubeDL

SRC = sys.argv[1] if len(sys.argv) > 1 else "put your .csv here"
OUT = "youtube_direct_links.csv"

BAD = ["official video", "music video", "(video)", "[video]", "live", "lyric",
       "cover", "reaction", "remix", "karaoke", "visualizer", "behind the scenes"]

def score(entry):
    title = (entry.get("title") or "").lower()
    chan = (entry.get("channel") or entry.get("uploader") or "").lower()
    if any(b in title for b in BAD) and "official audio" not in title:
        return -1
    s = 0
    if chan.endswith("- topic"):
        s += 10
    if "official audio" in title:
        s += 8
    elif "audio" in title:
        s += 4
    return s

with open(SRC, newline="", encoding="utf-8-sig") as sf:
    rows = list(csv.DictReader(sf))
done = set()
if os.path.exists(OUT):
    with open(OUT, newline="", encoding="utf-8") as of:
        done = {x["Track URI"] for x in csv.DictReader(of)}

new_file = not os.path.exists(OUT)
opts = {"quiet": True, "skip_download": True, "extract_flat": True}
with YoutubeDL(opts) as ydl, open(OUT, "a", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    if new_file:
        w.writerow(["Track URI", "Track", "Artist", "YouTube Link", "Matched Title", "Confidence"])
    for r in rows:
        if r["Track URI"] in done:
            continue
        artist = str(r["Artist Name(s)"]).replace(";", ", ")
        track = str(r["Track Name"])
        try:
            res = ydl.extract_info(f"ytsearch6:{artist} {track} official audio", download=False)
            entries = res.get("entries", [])
        except Exception:
            entries = []
        best = max(entries, key=score, default=None)
        if best and score(best) >= 0:
            conf = "high" if score(best) >= 8 else "medium" if score(best) >= 4 else "low"
            w.writerow([r["Track URI"], track, artist, best["url"], best.get("title", ""), conf])
        else:
            w.writerow([r["Track URI"], track, artist, "", "", "not found"])
        f.flush()
        print(f"{artist} - {track}")