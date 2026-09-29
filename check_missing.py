import re
import sys
from pathlib import Path

URLS_FILE = "urls.txt"
DOWNLOAD_DIR = sys.argv[1] if len(sys.argv) > 1 else "."

ID_RE = re.compile(
    r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})"
)

def get_id(url):
    m = ID_RE.search(url)
    return m.group(1) if m else None

# Collect filenames from the download folder (recursive)
files = [p.name for p in Path(DOWNLOAD_DIR).rglob("*") if p.is_file()]

missing = []
for line in Path(URLS_FILE).read_text(encoding="utf-8").splitlines():
    url = line.strip()
    if not url or url.startswith("#"):
        continue
    vid = get_id(url)
    if not vid:
        print(f"[!] Couldn't parse ID: {url}")
        continue
    if not any(vid in f for f in files):
        missing.append(url)

print(f"\nMissing: {len(missing)}")
for u in missing:
    print(u)

Path("missing.txt").write_text("\n".join(missing), encoding="utf-8")
