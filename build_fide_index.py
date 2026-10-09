#!/usr/bin/env python3
"""Sorts every over-the-board game broadcast on Lichess by FIDE ID, for Fox Chess Lab's "FIDE player" search.

Source: the Lichess open database's monthly broadcast files (https://database.lichess.org/#broadcasts),
released under CC0. Every game there carries both players' FIDE IDs in its headers (WhiteFideId / BlackFideId).

Output (the folder published on GitHub Pages):
  p/XYZ.json.gz   one file per group of players (FIDE ID modulo 4096, as 3 hex digits), holding their games
  meta.json       which months are in, when it was built, how many games and players
  index.html      a short page saying what this is

  python build_fide_index.py --mode full   --out out               everything from January 2020
  python build_fide_index.py --mode update --prev prev --out out   only months not in prev/meta.json yet

Needs Python 3.9+ and the "zstandard" package (pip install zstandard).
"""
import argparse, collections, gzip, hashlib, json, os, re, shutil, sys, tempfile, time, unicodedata, urllib.request

BASE = "https://database.lichess.org/broadcast/"
SHARDS = 4096                    # players are grouped into this many files
PLIES = 30                       # opening moves kept per game (15 moves each side); the full game is on Lichess
FLUSH_EVERY = 200_000            # lines buffered in memory before they're written to the scratch folder
UA = "fox-chess-data/1 (+https://github.com/Batancr/fox-chess-data)"
VERSION = 3

def log(*a):
    print(*a, flush=True)

# ---------------------------------------------------------------- downloads
def get(url, tries=5):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - any network error is retried
            if k == tries - 1:
                raise
            wait = 10 * (k + 1)
            log(f"  {url}: {e}; trying again in {wait}s")
            time.sleep(wait)

def download(url, dest, tries=5):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except Exception as e:  # noqa: BLE001
            if k == tries - 1:
                raise
            wait = 20 * (k + 1)
            log(f"  {url}: {e}; trying again in {wait}s")
            time.sleep(wait)

def available_months(base):
    """Months Lichess has published, from its counts.txt (lines like 'lichess_db_broadcast_2026-09.pgn.zst 28638')."""
    txt = get(base + "counts.txt").decode("utf8", "replace")
    months = sorted(set(re.findall(r"lichess_db_broadcast_(\d{4}-\d{2})\.pgn\.zst", txt)))
    if not months:
        raise SystemExit("Couldn't read the list of months from " + base + "counts.txt")
    return months

def checksums(base):
    try:
        txt = get(base + "sha256sums.txt").decode("utf8", "replace")
    except Exception as e:  # noqa: BLE001
        log("  (no checksum list:", e, ")")
        return {}
    out = {}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

# ---------------------------------------------------------------- reading PGN
HEADER = re.compile(r'^\[(\w+)\s+"((?:[^"\\]|\\.)*)"\]\s*$')
COMMENT = re.compile(r"\{[^}]*\}")
NOISE = re.compile(r"\$\d+|\d+\.(?:\.\.)?|\b(?:1-0|0-1|1/2-1/2|\*)(?=\s|$)")
RESULTS = {"1-0": (1, 0), "0-1": (0, 1), "1/2-1/2": (0.5, 0.5)}

def games(stream):
    """(headers, movetext) for each game in a PGN text stream."""
    headers, moves = {}, []
    for line in stream:
        line = line.rstrip("\r\n")
        if line.startswith("["):
            if moves:
                yield headers, " ".join(moves)
                headers, moves = {}, []
            m = HEADER.match(line)
            if m:
                headers[m.group(1)] = m.group(2).replace('\\"', '"')
        elif line.strip():
            moves.append(line.strip())
    if headers or moves:
        yield headers, " ".join(moves)

def sans(movetext, limit):
    t = COMMENT.sub(" ", movetext)
    if "(" in t:                                  # drop side lines, which can be nested
        out, depth = [], 0
        for ch in t:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif not depth:
                out.append(ch)
        t = "".join(out)
    t = NOISE.sub(" ", t)
    toks = [x.rstrip("!?") for x in t.split()]
    toks = [x for x in toks if x]
    return toks[:limit], len(toks)

MOVE_COUNTS = {20, 30, 36, 40, 50, 60}

def tc_seconds(tc):
    """(starting seconds, increment seconds) from a broadcast's TimeControl, or None. Broadcasts write it many ways:
    '5400+30', '40/5400+30:1800+30', '90+30' (minutes), "3'+2''", '90min/40 moves + 30min + 30s/move',
    '100 minutes/40 moves + 40 minutes/rest with 30 seconds increment', 'G/90; +30', '1:30:30' (h:mm:ss) ..."""
    t = (tc or "").lower().replace("’", "'").replace("”", '"').replace("''", '"').replace("“", '"')
    m = re.fullmatch(r"\s*(\d):(\d\d)(?::(\d\d))?\s*", t)
    if m:                                                  # 1:30:30 = 1 h 30 min, 30 s increment
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60, int(m.group(3) or 0)
    t = re.split(r"\(|\s/\s|(?<=\d):(?=\d)", t)[0]          # drop alternatives and later time periods
    t = re.sub(r"(\d+)\s*/\s*(\d+)(?!\s*(?:min|h|'))",       # '40/90', '90/40', '5400/40': drop the move count
               lambda m: m.group(1) if int(m.group(2)) in MOVE_COUNTS else m.group(2) if int(m.group(1)) in MOVE_COUNTS else m.group(1), t)
    t = re.sub(r"/\s*\d+\s*(?:moves?|mv\b|m\b)|\d+\s*(?:moves?|mv\b)", " ", t)   # '40 moves', '/40m' (but '45m' is minutes)
    t = re.sub(r"\bx\s*\d+", " ", t)                        # "90' x 40"
    t = re.sub(r"(?:from\s+)?move\s*(?:\d+|one)", " ", t)  # 'from move 1'
    base = inc = None
    for mt in re.finditer(r"(\+)?\s*(\d+)\s*(hours?|hrs?|h\b|minutes?|mins?|min\b\.?|m\b|'|seconds?|secs?|sec\b\.?|segs?|seg\b|s\b|\")?", t):
        n, u, plus = int(mt.group(2)), (mt.group(3) or "").rstrip("."), bool(mt.group(1))
        if base is None:
            base = n * 3600 if u[:1] == "h" else n * 60 if (u[:1] == "m" or u == "'") else n if u else (n * 60 if n <= 150 else n)
            continue
        if u in ("s", '"') or u.startswith("sec") or u.startswith("seg"):
            inc = n
            break
        if not u and plus and inc is None:
            inc = n                                          # '90+30': a bare number after + is the increment
    return (base, inc or 0) if base else None

def speed(tc, event=""):
    """Lichess's speed classes (b bullet, z blitz, r rapid, c classical) from the TimeControl: starting time plus
    40 times the increment. Without a usable TimeControl, the event's name decides ('... Rapid', '... Blitz')."""
    r = tc_seconds(tc)
    if r:
        est = r[0] + 40 * r[1]
        return "u" if est < 30 else "b" if est < 180 else "z" if est < 480 else "r" if est < 1500 else "c"
    e = (event or "").lower()
    bl, ra = "blitz" in e, "rapid" in e
    if bl != ra:
        return "z" if bl else "r"
    if not bl and re.search(r"classical|standard", e):
        return "c"
    return ""

ONLINE = re.compile(r"\bonline\b|chess\.com|chess24|titled (?:tuesday|arena)|magnus (?:carlsen )?invitational|lindores abbey|"
                    r"chessable masters|airthings masters|opera euro rapid|new in chess classic|"
                    r"goldmoney asian|skilling open|speed chess championship|pro chess league|bullet chess championship", re.I)

def looks_online(event):
    """Some 2020-2021 broadcasts relayed online events; these are marked so the site can leave them out."""
    return 1 if ONLINE.search(event or "") else 0

def tc_shape(tc):
    return re.sub(r"\d+", "N", (tc or "").strip())[:40] or "(none)"

def norm_name(s):
    """'Carlsen, Magnus', 'Magnus Carlsen' and 'CARLSEN Magnus' all become 'carlsen magnus'. Needs two name parts."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    toks = sorted(re.findall(r"[^\W\d_]+", s))
    return " ".join(toks) if len(toks) >= 2 and sum(len(x) > 1 for x in toks) >= 2 else ""

def fide(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() and int(v) > 0 else 0

def elo(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else 0

def date_num(h, month=""):
    for k in ("UTCDate", "Date"):
        d = h.get(k, "")
        m = re.match(r"^(\d{4})\.(\d{2})\.(\d{2})", d)
        if m and "?" not in d:
            return int(m.group(1) + m.group(2) + m.group(3))
    if month:                                   # no usable date: the month of the file it came from (day 00)
        return int(month.replace("-", "")) * 100
    m = re.match(r"^(\d{4})", h.get("Date", ""))
    return int(m.group(1)) * 10000 if m else 0

def round_and_game(h):
    """('tour-slug/round-slug/roundId', 'gameId') from the GameURL header."""
    u = h.get("GameURL", "")
    m = re.search(r"/broadcast/([^/]+/[^/]+/[A-Za-z0-9]{8})/([A-Za-z0-9]{8})", u)
    if m:
        return m.group(1), m.group(2)
    u = h.get("BroadcastURL", "")
    m = re.search(r"/broadcast/([^/]+/[^/]+/[A-Za-z0-9]{8})", u)
    return (m.group(1) if m else ""), ""

def shard_of(fid):
    return "%03x" % (fid % SHARDS)

# ---------------------------------------------------------------- one month into the scratch folder
class Scratch:
    """Game lines per shard, written to scratch files in batches so memory stays small."""
    def __init__(self, folder):
        self.folder, self.buf, self.n, self.touched = folder, {}, 0, set()
        os.makedirs(folder, exist_ok=True)
    def add(self, shard, line):
        self.buf.setdefault(shard, []).append(line)
        self.touched.add(shard)
        self.n += 1
        if self.n >= FLUSH_EVERY:
            self.flush()
    def flush(self):
        for s, lines in self.buf.items():
            with open(os.path.join(self.folder, s + ".jsonl"), "a", encoding="utf8") as f:
                f.write("\n".join(lines) + "\n")
        self.buf, self.n = {}, 0

class Names:
    """Which FIDE ID each player name belongs to, learnt from games that carry FIDE IDs (Lichess added them to
    broadcasts in 2023). Used to place older games that have names but no IDs, only when a name means exactly one ID."""
    def __init__(self):
        self.ids = collections.defaultdict(set)
    def learn(self, name, fid):
        k = norm_name(name)
        if k:
            self.ids[k].add(fid)
    def find(self, name):
        s = self.ids.get(norm_name(name))
        return next(iter(s)) if s and len(s) == 1 else 0
    def load(self, path):
        if os.path.exists(path):
            with gzip.open(path, "rt", encoding="utf8") as f:
                for k, v in json.load(f).items():
                    self.ids[k].update(v)
    def save(self, path):
        body = json.dumps({k: sorted(v) for k, v in sorted(self.ids.items())}, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        with open(path, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as f:
                f.write(body)

def read_month(path, month, scratch, removed, names, noid, tcs):
    import zstandard   # imported here so --help works without it
    import io
    kept = waiting = skipped = 0
    with open(path, "rb") as fh:
        reader = zstandard.ZstdDecompressor().stream_reader(fh)
        text = io.TextIOWrapper(reader, encoding="utf8", errors="replace")
        for h, mt in games(text):
            res = RESULTS.get(h.get("Result", ""))
            if (not res or (h.get("Variant") and h.get("Variant").lower() not in ("standard", "chess"))
                    or h.get("FEN") or h.get("SetUp") == "1"
                    or "BOT" in (h.get("WhiteTitle", ""), h.get("BlackTitle", ""))):
                skipped += 1
                continue
            mv, n = sans(mt, PLIES)
            if not mv:
                skipped += 1
                continue
            wf, bf = fide(h.get("WhiteFideId")), fide(h.get("BlackFideId"))
            rnd, gid = round_and_game(h)
            event = h.get("BroadcastName") or h.get("Event") or ""
            sp = speed(h.get("TimeControl", ""), event)
            tcs[(tc_shape(h.get("TimeControl", "")), sp)][0] += 1
            tcs[(tc_shape(h.get("TimeControl", "")), sp)][1] = h.get("TimeControl", "")
            base = {"d": date_num(h, month), "s": sp, "n": n, "m": " ".join(mv),
                    "rp": rnd, "rn": event, "g": gid, "eco": h.get("ECO", ""), "x": 0, "on": looks_online(event)}
            for me, them, c in (("White", "Black", "w"), ("Black", "White", "b")):
                fid = wf if c == "w" else bf
                g = dict(base, c=c, r=res[0] if c == "w" else res[1],
                         o=h.get(them, "?"), of=bf if c == "w" else wf, oe=elo(h.get(them + "Elo")),
                         e=elo(h.get(me + "Elo")))
                if fid:
                    names.learn(h.get(me, ""), fid)
                    if fid not in removed:
                        scratch.add(shard_of(fid), json.dumps([fid, h.get(me, "?"), h.get(me + "Title", ""), g], ensure_ascii=False, separators=(",", ":")))
                else:                                       # no FIDE ID on this side: try its name once every month is read
                    noid.write(json.dumps([h.get(me, ""), h.get(me + "Title", ""), g], ensure_ascii=False, separators=(",", ":")) + "\n")
            if wf or bf:
                kept += 1
            else:
                waiting += 1
    return kept, waiting, skipped

def place_by_name(noid_path, scratch, removed, names):
    """Games (or sides) without a FIDE ID, placed by name where the name belongs to exactly one FIDE ID."""
    placed = left = 0
    if not os.path.exists(noid_path):
        return 0, 0
    with open(noid_path, encoding="utf8") as f:
        for line in f:
            name, title, g = json.loads(line)
            fid = names.find(name)
            if not fid or fid in removed:
                left += 1
                continue
            if not g["of"]:
                g["of"] = names.find(g["o"])
            g["x"] = 1
            scratch.add(shard_of(fid), json.dumps([fid, name, title, g], ensure_ascii=False, separators=(",", ":")))
            placed += 1
    return placed, left

# ---------------------------------------------------------------- merging into the published files
# a game in a published file: [gameId, yyyymmdd, colour, result, opponent, opponentFideId, opponentElo, ownElo,
#                              roundIndex, speed, plies, "first moves", eco]
def load_shard(path):
    if not os.path.exists(path):
        return {}
    with gzip.open(path, "rt", encoding="utf8") as f:
        data = json.load(f)
    rounds = data.get("rounds", [])
    players = {}
    for fid, p in data.get("players", {}).items():
        gl = []
        for x in p["games"]:
            r = rounds[x[8]] if 0 <= x[8] < len(rounds) else ["", ""]
            gl.append({"g": x[0], "d": x[1], "c": x[2], "r": x[3], "o": x[4], "of": x[5], "oe": x[6], "e": x[7],
                       "rn": r[0], "rp": r[1], "s": x[9], "n": x[10], "m": x[11], "eco": x[12] if len(x) > 12 else "",
                       "x": x[13] if len(x) > 13 else 0, "on": x[14] if len(x) > 14 else 0})
        players[int(fid)] = {"name": p.get("name", "?"), "title": p.get("title", ""), "seen": p.get("seen", 0), "games": gl}
    return players

def save_shard(path, players):
    rounds, ridx, out = [], {}, {}
    for fid in sorted(players):
        p = players[fid]
        rows = []
        for g in p["games"]:
            key = (g["rn"], g["rp"])
            if key not in ridx:
                ridx[key] = len(rounds)
                rounds.append([g["rn"], g["rp"]])
            rows.append([g["g"], g["d"], g["c"], g["r"], g["o"], g["of"], g["oe"], g["e"], ridx[key], g["s"], g["n"], g["m"], g["eco"], g.get("x", 0), g.get("on", 0)])
        out[str(fid)] = {"name": p["name"], "title": p["title"], "seen": p["seen"], "games": rows}
    body = json.dumps({"v": VERSION, "rounds": rounds, "players": out}, ensure_ascii=False, separators=(",", ":")).encode("utf8")
    with open(path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as f:   # mtime=0: unchanged data gives identical bytes
            f.write(body)

def game_key(g):
    if g["g"]:
        return (g["rp"], g["g"])
    return (g["rp"], g["d"], g["o"], g["c"], g["m"])

def merge_shard(shard, prev_dir, out_dir, scratch_dir, removed):
    path = os.path.join(out_dir, "p", shard + ".json.gz")
    players = load_shard(os.path.join(prev_dir, "p", shard + ".json.gz")) if prev_dir else {}
    sp = os.path.join(scratch_dir, shard + ".jsonl")
    if os.path.exists(sp):
        with open(sp, encoding="utf8") as f:
            for line in f:
                if not line.strip():
                    continue
                fid, name, title, g = json.loads(line)
                p = players.setdefault(fid, {"name": name, "title": title, "seen": 0, "games": []})
                if not g.get("x") and g["d"] >= p["seen"]:  # keep the name and title from their latest game with a FIDE ID
                    p["name"], p["title"], p["seen"] = name, title, g["d"]
                p["games"].append(g)
    for fid in list(players):
        if fid in removed:
            del players[fid]
            continue
        seen, gl = set(), []
        for g in sorted(players[fid]["games"], key=lambda g: (-g["d"], g["rp"], g["g"], g.get("x", 0))):
            k = game_key(g)
            if k not in seen:
                seen.add(k)
                gl.append(g)
        players[fid]["games"] = gl
    if players:
        save_shard(path, players)
    elif os.path.exists(path):
        os.remove(path)
    return len(players), sum(len(p["games"]) for p in players.values())

# ---------------------------------------------------------------- the published folder
INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fox Chess Lab: FIDE game files</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;color:#222;background:#fff}code{background:#f2f2f2;padding:1px 4px}</style></head><body>
<h1>Fox Chess Lab: FIDE game files</h1>
<p>Over-the-board games broadcast on Lichess, sorted by FIDE ID, for the <a href="https://batancr.github.io/fox-chess-lab/">Fox Chess Lab</a> player search.
Each file holds the players whose FIDE ID gives that number when divided by 4096 (the file name is the remainder in hexadecimal), with each game's opening moves and a link to the full game on Lichess.</p>
<p>Built from the <a href="https://database.lichess.org/#broadcasts">Lichess open database</a> (released under CC0). Months included and totals: <a href="meta.json">meta.json</a>.</p>
<p>To ask for a player to be removed, open an issue at <a href="https://github.com/Batancr/fox-chess-data/issues">github.com/Batancr/fox-chess-data</a>.</p>
</body></html>
"""

def read_removed(path):
    out = set()
    if path and os.path.exists(path):
        for line in open(path, encoding="utf8"):
            line = line.split("#")[0].strip()
            if line.isdigit():
                out.add(int(line))
    return out

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["full", "update"], default="update")
    ap.add_argument("--prev", help="the folder published last time (for --mode update)")
    ap.add_argument("--out", required=True, help="where to write the new folder")
    ap.add_argument("--base-url", default=BASE, help="where the monthly files are (for testing)")
    ap.add_argument("--from-month", default="2020-01")
    ap.add_argument("--max-months", type=int, default=0, help="stop after this many months (0 = no limit)")
    ap.add_argument("--removed", default="removed.txt", help="FIDE IDs to leave out, one per line")
    ap.add_argument("--work", default="", help="scratch folder (default: a temporary one)")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds to wait between downloads")
    a = ap.parse_args()
    t0 = time.time()
    base = a.base_url if a.base_url.endswith("/") else a.base_url + "/"
    removed = read_removed(a.removed)

    prev_meta = {}
    prev = a.prev if a.mode == "update" and a.prev and os.path.exists(os.path.join(a.prev, "meta.json")) else None
    if prev:
        prev_meta = json.load(open(os.path.join(prev, "meta.json"), encoding="utf8"))
        if prev_meta.get("version") != VERSION or prev_meta.get("plies") != PLIES or prev_meta.get("shards") != SHARDS:
            log("The published files use a different format; rebuilding everything.")
            prev, prev_meta = None, {}
    elif a.mode == "update":
        log("Nothing published yet; building everything.")

    done = set(prev_meta.get("months", []))
    months = [m for m in available_months(base) if m >= a.from_month and m not in done]
    if a.max_months:
        months = months[: a.max_months]
    log(f"Months to add: {len(months)}" + (f" ({months[0]} to {months[-1]})" if months else ""))

    os.makedirs(os.path.join(a.out, "p"), exist_ok=True)
    work = a.work or tempfile.mkdtemp(prefix="fide-")
    scratch = Scratch(os.path.join(work, "scratch"))
    names = Names()
    if prev:
        names.load(os.path.join(prev, "names.json.gz"))
    noid_path = os.path.join(work, "noid.jsonl")
    noid = open(noid_path, "w", encoding="utf8")
    tcs = collections.defaultdict(lambda: [0, ""])
    sums = checksums(base) if months else {}
    stats = dict(prev_meta.get("month_games", {}))
    nostats = dict(prev_meta.get("month_no_id", {}))
    for i, m in enumerate(months):
        name = f"lichess_db_broadcast_{m}.pgn.zst"
        dest = os.path.join(work, name)
        t1 = time.time()
        log(f"[{i + 1}/{len(months)}] {m}: downloading…")
        download(base + name, dest)
        size = os.path.getsize(dest)
        if name in sums and sha256(dest) != sums[name]:
            raise SystemExit(f"{name}: the checksum doesn't match Lichess's list. Stopping so nothing wrong is published.")
        kept, waiting, skipped = read_month(dest, m, scratch, removed, names, noid, tcs)
        os.remove(dest)
        stats[m], nostats[m] = kept, waiting
        log(f"    {size / 1e6:.1f} MB: {kept:,} games with a FIDE ID, {waiting:,} without (placed by name later if possible), "
            f"{skipped:,} left out, {time.time() - t1:.0f}s")
        if a.pause and i < len(months) - 1:
            time.sleep(a.pause)
    noid.close()
    placed, left = place_by_name(noid_path, scratch, removed, names)
    if months:
        log(f"Placed {placed:,} player-games without a FIDE ID by their name; {left:,} couldn't be (unknown or shared names).")
    scratch.flush()

    # copy last time's files, then rewrite only the groups that changed (new games, or a removed player)
    if prev:
        for f in os.listdir(os.path.join(prev, "p")):
            if f.endswith(".json.gz"):
                shutil.copyfile(os.path.join(prev, "p", f), os.path.join(a.out, "p", f))
    counts = dict(prev_meta.get("counts", {})) if prev else {}
    todo = set(scratch.touched) | {shard_of(x) for x in removed}
    log(f"Writing {len(todo):,} of {SHARDS:,} files…")
    for k, s in enumerate(sorted(todo)):
        np_, ng = merge_shard(s, a.out if prev else None, a.out, scratch.folder, removed)
        if np_:
            counts[s] = [np_, ng]
        else:
            counts.pop(s, None)
        if k % 500 == 499:
            log(f"    {k + 1:,} files")
    shutil.rmtree(work, ignore_errors=True)
    names.save(os.path.join(a.out, "names.json.gz"))

    size = sum(os.path.getsize(os.path.join(a.out, "p", f)) for f in os.listdir(os.path.join(a.out, "p")))
    meta = {"version": VERSION, "shards": SHARDS, "plies": PLIES, "source": base,
            "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "months": sorted(done | set(months)), "month_games": stats, "month_no_id": nostats,
            "placed_by_name": prev_meta.get("placed_by_name", 0) + placed,
            "tc_formats": [[k[0], k[1], v[0], v[1]] for k, v in sorted(tcs.items(), key=lambda x: -x[1][0])[:60]] or prev_meta.get("tc_formats", []),
            "players": sum(c[0] for c in counts.values()), "entries": sum(c[1] for c in counts.values()),
            "bytes": size, "counts": counts,
            "format": "p/XYZ.json.gz: XYZ = FIDE ID % 4096 in 3 hex digits. players[id].games rows: "
                      "[gameId, yyyymmdd, colour, result, opponent, opponentFideId, opponentElo, ownElo, roundIndex, "
                      "speed (b bullet, z blitz, r rapid, c classical, '' unknown), plies, first moves, ECO, "
                      "1 if placed by the player's name (the game had no FIDE ID for them) else 0, 1 if the event looks online else 0]; "
                      "rounds[i] = [event name, 'tour-slug/round-slug/roundId'] -> https://lichess.org/broadcast/<that>/<gameId>"}
    with open(os.path.join(a.out, "meta.json"), "w", encoding="utf8") as f:
        json.dump(meta, f, indent=1)
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf8") as f:
        f.write(INDEX)
    open(os.path.join(a.out, ".nojekyll"), "w").close()
    log(f"Done in {(time.time() - t0) / 60:.1f} min: {meta['players']:,} players, {meta['entries']:,} player-games, "
        f"{size / 1e6:.1f} MB in {len(counts):,} files.")

if __name__ == "__main__":
    main()
