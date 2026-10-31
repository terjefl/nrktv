"""What we follow (sources) and every item we know of, in SQLite.

The web page and the download loop run in separate threads, so every call opens its own
connection; SQLite in WAL mode handles the rest.
"""

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,          -- nrk | youtube
    kind TEXT NOT NULL,              -- series | program (NRK); channel | playlist (YouTube)
    ref TEXT NOT NULL,               -- NRK series slug / program id; YouTube channel or playlist id
    title TEXT NOT NULL,
    plot TEXT NOT NULL DEFAULT '',
    series_type TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'all', -- all | seasons | new
    seasons TEXT NOT NULL DEFAULT '[]',
    keep_days INTEGER,
    since TEXT NOT NULL,             -- when we started following; mode 'new' takes what comes after
    enabled INTEGER NOT NULL DEFAULT 1,
    image TEXT,
    poster TEXT,
    backdrop TEXT,
    layout TEXT NOT NULL DEFAULT 'tv', -- tv | videos | audio | movie
    library TEXT NOT NULL DEFAULT '',  -- YouTube: folder under youtube_dir, one Plex/Jellyfin library
    folder TEXT NOT NULL DEFAULT '',   -- the source's own folder name; '' = its title
    url TEXT NOT NULL DEFAULT '',
    cutoff TEXT,                       -- only items published on or after this date
    title_regex TEXT NOT NULL DEFAULT '',
    min_duration INTEGER,              -- seconds
    max_duration INTEGER,
    shorts INTEGER NOT NULL DEFAULT 0,
    livestreams INTEGER NOT NULL DEFAULT 1,
    sponsorblock TEXT NOT NULL DEFAULT '', -- '' | mark | remove
    sb_categories TEXT NOT NULL DEFAULT 'sponsor,selfpromo,interaction',
    subtitles TEXT NOT NULL DEFAULT '',    -- languages, e.g. 'nb,en'; NRK always takes its own
    cookies INTEGER NOT NULL DEFAULT 0,
    quality INTEGER NOT NULL DEFAULT 2160, -- max height; 0 = audio only
    feed_token TEXT,
    last_full_index REAL NOT NULL DEFAULT 0,
    last_checked REAL NOT NULL DEFAULT 0,
    last_aired TEXT,                   -- NRK series: the latest episode out
    next_aired TEXT,                   -- and the next one NRK lists
    UNIQUE (provider, ref)
);
CREATE TABLE IF NOT EXISTS seasons (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    scanned REAL NOT NULL,
    PRIMARY KEY (source_id, name)
);
CREATE TABLE IF NOT EXISTS items (
    provider TEXT NOT NULL,
    item_id TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    title TEXT NOT NULL,
    plot TEXT NOT NULL DEFAULT '',
    aired TEXT,
    expires TEXT,
    image TEXT,
    year INTEGER,
    original_title TEXT,
    duration INTEGER,
    is_short INTEGER NOT NULL DEFAULT 0,
    is_live INTEGER NOT NULL DEFAULT 0,
    -- pending | done | failed | expired | geo | unavailable | skipped | removed
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    retry_after REAL NOT NULL DEFAULT 0,
    files TEXT NOT NULL DEFAULT '[]',
    size INTEGER NOT NULL DEFAULT 0,
    url TEXT,                        -- one-off downloads: the page it came from
    channel TEXT,                    -- one-off downloads: who published it (folder name)
    updated REAL NOT NULL,
    PRIMARY KEY (provider, item_id)
);
CREATE INDEX IF NOT EXISTS items_source ON items(source_id, season, episode);
"""

MAX_ATTEMPTS = 5

# What a source can be given when it is created or edited
SETTINGS = (
    "mode", "seasons", "keep_days", "layout", "library", "folder", "url", "cutoff", "title_regex",
    "min_duration", "max_duration", "shorts", "livestreams", "sponsorblock", "sb_categories", "subtitles",
    "cookies", "quality",
)
META = ("title", "plot", "series_type", "image", "poster", "backdrop", "last_aired", "next_aired")


@dataclass
class Source:
    id: int
    provider: str
    kind: str
    ref: str
    title: str
    plot: str = ""
    series_type: str = ""
    mode: str = "all"
    seasons: list[str] = field(default_factory=list)
    keep_days: int | None = None
    since: datetime = field(default_factory=lambda: datetime.now().astimezone())
    enabled: bool = True
    image: str | None = None
    poster: str | None = None
    backdrop: str | None = None
    layout: str = "tv"
    library: str = ""
    folder: str = ""
    url: str = ""
    cutoff: date | None = None
    title_regex: str = ""
    min_duration: int | None = None
    max_duration: int | None = None
    shorts: bool = False
    livestreams: bool = True
    sponsorblock: str = ""
    sb_categories: str = "sponsor,selfpromo,interaction"
    subtitles: str = ""
    cookies: bool = False
    quality: int = 2160
    feed_token: str | None = None
    last_full_index: float = 0.0
    last_checked: float = 0.0
    last_aired: date | None = None
    next_aired: date | None = None

    @property
    def dated(self) -> bool:
        """Numbered by date (season = year): NRK talk shows and news, and YouTube."""
        if self.provider == "youtube":
            return True
        return self.kind == "series" and self.series_type != "sequential"

    @property
    def folder_name(self) -> str:
        return self.folder or self.title

    def wants_season(self, name: str) -> bool:
        """For mode 'seasons': a chosen season, or for date series a month in a chosen year."""
        return self.mode != "seasons" or any(name == s or name.startswith(s) for s in self.seasons)


@dataclass
class Item:
    provider: str
    item_id: str
    source_id: int
    season: int
    episode: int
    title: str
    plot: str = ""
    aired: date | None = None
    expires: date | None = None
    image: str | None = None
    year: int | None = None
    original_title: str | None = None
    duration: int | None = None
    url: str | None = None
    channel: str | None = None
    is_short: bool = False
    is_live: bool = False
    status: str = "pending"
    attempts: int = 0
    error: str | None = None
    retry_after: float = 0.0
    files: list[str] = field(default_factory=list)
    size: int = 0
    updated: float = 0.0


def _source(r: sqlite3.Row) -> Source:
    d = dict(r)
    d["seasons"] = json.loads(d["seasons"])
    d["since"] = datetime.fromisoformat(d["since"])
    for key in ("cutoff", "last_aired", "next_aired"):
        d[key] = date.fromisoformat(d[key]) if d[key] else None
    for key in ("enabled", "shorts", "livestreams", "cookies"):
        d[key] = bool(d[key])
    return Source(**d)


def _item(r: sqlite3.Row) -> Item:
    d = {k: r[k] for k in r.keys() if k in _ITEM_FIELDS}
    d["aired"] = date.fromisoformat(d["aired"]) if d["aired"] else None
    d["expires"] = date.fromisoformat(d["expires"]) if d["expires"] else None
    d["files"] = json.loads(d["files"])
    d["is_short"], d["is_live"] = bool(d["is_short"]), bool(d["is_live"])
    return Item(**d)


_ITEM_FIELDS = {f.name for f in fields(Item)}


def _db_value(key: str, value):
    if key == "seasons":
        return json.dumps(value or [])
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'follows'").fetchone():
                _migrate_v1(c)
            c.executescript(SCHEMA)
            # v3: url and channel on items
            have = {r[1] for r in c.execute("PRAGMA table_info(items)")}
            for column in ("url", "channel"):
                if column not in have:
                    c.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
            # v4: last_aired and next_aired on sources
            have = {r[1] for r in c.execute("PRAGMA table_info(sources)")}
            for column in ("last_aired", "next_aired"):
                if column not in have:
                    c.execute(f"ALTER TABLE sources ADD COLUMN {column} TEXT")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    # ---- sources -------------------------------------------------------------

    def sources(self, enabled_only: bool = False, provider: str | None = None) -> list[Source]:
        where, args = [], []
        if enabled_only:
            where.append("enabled = 1")
        if provider:
            where.append("provider = ?")
            args.append(provider)
        sql = "SELECT * FROM sources" + (" WHERE " + " AND ".join(where) if where else "")
        with self.conn() as c:
            return [_source(r) for r in c.execute(sql + " ORDER BY title COLLATE NOCASE", args)]

    def source(self, source_id: int) -> Source | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _source(r) if r else None

    def source_by_ref(self, provider: str, ref: str) -> Source | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM sources WHERE provider = ? AND ref = ?", (provider, ref)).fetchone()
        return _source(r) if r else None

    def source_by_feed(self, token: str) -> Source | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM sources WHERE feed_token = ?", (token,)).fetchone()
        return _source(r) if r else None

    def save_source(self, provider: str, kind: str, ref: str, title: str, **values) -> Source:
        """Start following, or change how an existing source works (its items stay). values:
        any of SETTINGS (how to follow) and META (what the provider says about it)."""
        unknown = set(values) - set(SETTINGS) - set(META)
        if unknown:
            raise TypeError(f"Unknown source fields: {sorted(unknown)}")
        settings = {k: _db_value(k, v) for k, v in values.items() if k in SETTINGS}
        with self.conn() as c:
            row = c.execute("SELECT id FROM sources WHERE provider = ? AND ref = ?", (provider, ref)).fetchone()
            if row:
                sid = row["id"]
                if settings:
                    sets = ", ".join(f"{k} = ?" for k in settings)
                    c.execute(f"UPDATE sources SET {sets}, enabled = 1 WHERE id = ?", (*settings.values(), sid))
            else:
                cols = ["provider", "kind", "ref", "title", "since", "feed_token", *settings]
                vals = [provider, kind, ref, title, datetime.now().astimezone().isoformat(),
                        secrets.token_urlsafe(18), *settings.values()]
                cur = c.execute(
                    f"INSERT INTO sources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
                )
                sid = cur.lastrowid
            # The choice of seasons decides what gets scanned: look at every season again
            c.execute("DELETE FROM seasons WHERE source_id = ?", (sid,))
        meta = {k: v for k, v in values.items() if k in META}
        if meta:
            self.update_meta(sid, **meta)
        return self.source(sid)

    def update_meta(self, source_id: int, **values) -> None:
        values = {k: _db_value(k, v) for k, v in values.items() if k in META}
        if not values:
            return
        with self.conn() as c:
            sets = ", ".join(f"{k} = ?" for k in values)
            c.execute(f"UPDATE sources SET {sets} WHERE id = ?", (*values.values(), source_id))

    def touch(self, source_id: int, full_index: bool = False) -> None:
        now = time.time()
        with self.conn() as c:
            c.execute("UPDATE sources SET last_checked = ? WHERE id = ?", (now, source_id))
            if full_index:
                c.execute("UPDATE sources SET last_full_index = ? WHERE id = ?", (now, source_id))

    def set_enabled(self, source_id: int, enabled: bool) -> None:
        with self.conn() as c:
            c.execute("UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))

    def delete_source(self, source_id: int) -> None:
        """Stop following. Downloaded files stay in the library."""
        with self.conn() as c:
            c.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    # ---- seasons -------------------------------------------------------------

    def scanned(self, source_id: int) -> dict[str, float]:
        with self.conn() as c:
            rows = c.execute("SELECT name, scanned FROM seasons WHERE source_id = ?", (source_id,))
            return {r["name"]: r["scanned"] for r in rows}

    def mark_scanned(self, source_id: int, name: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO seasons (source_id, name, scanned) VALUES (?, ?, ?) "
                "ON CONFLICT DO UPDATE SET scanned = excluded.scanned",
                (source_id, name, time.time()),
            )

    # ---- items ---------------------------------------------------------------

    def known_ids(self, source_id: int) -> set[str]:
        with self.conn() as c:
            return {r[0] for r in c.execute("SELECT item_id FROM items WHERE source_id = ?", (source_id,))}

    def add_item(self, item: Item, unique_number: bool = False, number_step: int = 1) -> bool:
        """Record a newly found item; False if we knew it. Season/episode numbers of known items
        never change: their files are named by them. With unique_number, an item whose number is
        taken in its season gets the next free one (number_step -1 counts down, Pinchflat-style)."""
        with self.conn() as c:
            if c.execute(
                "SELECT 1 FROM items WHERE provider = ? AND item_id = ?", (item.provider, item.item_id)
            ).fetchone():
                if item.expires:  # rights can be extended; keep the latest end date
                    c.execute(
                        "UPDATE items SET expires = ? WHERE provider = ? AND item_id = ?",
                        (item.expires.isoformat(), item.provider, item.item_id),
                    )
                return False
            number = item.episode
            if unique_number:
                taken = {
                    r[0]
                    for r in c.execute(
                        "SELECT episode FROM items WHERE source_id = ? AND season = ?", (item.source_id, item.season)
                    )
                }
                while number in taken:
                    number += number_step
            values = {f.name: _db_value(f.name, getattr(item, f.name)) for f in fields(Item)}
            values["episode"] = number
            values["files"] = json.dumps(item.files)
            values["updated"] = item.updated or time.time()
            cols = list(values)
            c.execute(f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(values.values()))
            return True

    def items(self, source_id: int) -> list[Item]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE source_id = ? ORDER BY season DESC, episode DESC, aired DESC", (source_id,)
            )
            return [_item(r) for r in rows]

    def item(self, provider: str, item_id: str) -> Item | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM items WHERE provider = ? AND item_id = ?", (provider, item_id)).fetchone()
        return _item(r) if r else None

    def due(self) -> list[Item]:
        """Items to download now: one-off downloads first (someone is waiting for them), then NRK,
        those whose rights end first first, then the rest oldest first."""
        now = time.time()
        with self.conn() as c:
            rows = c.execute(
                """SELECT i.* FROM items i JOIN sources s ON s.id = i.source_id
                   WHERE s.enabled = 1 AND ((i.status = 'pending' AND i.retry_after <= ?)
                       OR (i.status = 'failed' AND i.attempts < ? AND i.retry_after <= ?))
                   ORDER BY i.provider != 'adhoc', i.expires IS NULL, i.expires, i.provider = 'youtube', i.aired,
                       i.season, i.episode""",
                (now, MAX_ATTEMPTS, now),
            )
            return [_item(r) for r in rows]

    def set_status(
        self, item: Item, status: str, error: str | None = None, files: list[str] | None = None, size: int | None = None
    ) -> None:
        key = (item.provider, item.item_id)
        with self.conn() as c:
            if status == "failed":
                attempts = c.execute(
                    "SELECT attempts FROM items WHERE provider = ? AND item_id = ?", key
                ).fetchone()[0] + 1
                # an hour, then 2, 4, 8 ...
                retry_after = time.time() + 3600 * 2 ** (attempts - 1)
                c.execute(
                    "UPDATE items SET status = ?, error = ?, attempts = ?, retry_after = ?, updated = ? "
                    "WHERE provider = ? AND item_id = ?",
                    (status, (error or "")[-500:], attempts, retry_after, time.time(), *key),
                )
                return
            sets, args = ["status = ?", "error = ?", "updated = ?"], [status, error, time.time()]
            if files is not None:
                sets.append("files = ?")
                args.append(json.dumps(files))
            if size is not None:
                sets.append("size = ?")
                args.append(size)
            c.execute(f"UPDATE items SET {', '.join(sets)} WHERE provider = ? AND item_id = ?", (*args, *key))

    def postpone(self, item: Item, until: float, error: str | None = None) -> None:
        """Not out yet, or the host wants us back later: try again then, without using up an
        attempt. error: shown on the page (None for 'not out yet', which is no problem)."""
        with self.conn() as c:
            c.execute(
                "UPDATE items SET status = 'pending', error = ?, retry_after = ?, updated = ? "
                "WHERE provider = ? AND item_id = ?",
                (error, until, time.time(), item.provider, item.item_id),
            )

    def retry(self, provider: str, item_id: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE items SET status = 'pending', attempts = 0, retry_after = 0, error = NULL, updated = ? "
                "WHERE provider = ? AND item_id = ?",
                (time.time(), provider, item_id),
            )

    def skip(self, provider: str, item_id: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE items SET status = 'skipped', error = 'hoppet over manuelt', updated = ? "
                "WHERE provider = ? AND item_id = ?",
                (time.time(), provider, item_id),
            )

    def update_files(self, item: Item, files: list[str]) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE items SET files = ? WHERE provider = ? AND item_id = ?",
                (json.dumps(files), item.provider, item.item_id),
            )

    def counts(self) -> dict[int, dict[str, int]]:
        """Items per status, per source."""
        result: dict[int, dict[str, int]] = {}
        with self.conn() as c:
            for r in c.execute("SELECT source_id, status, COUNT(*) n FROM items GROUP BY source_id, status"):
                result.setdefault(r["source_id"], {})[r["status"]] = r["n"]
        return result

    def sizes(self) -> dict[int, int]:
        """Bytes downloaded, per source."""
        with self.conn() as c:
            rows = c.execute("SELECT source_id, SUM(size) n FROM items WHERE status = 'done' GROUP BY source_id")
            return {r["source_id"]: r["n"] or 0 for r in rows}

    def next_expiry(self) -> dict[int, date]:
        """The first date an item we still want disappears, per source."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT source_id, MIN(expires) d FROM items WHERE expires IS NOT NULL "
                "AND status IN ('pending', 'failed') GROUP BY source_id"
            )
            return {r["source_id"]: date.fromisoformat(r["d"]) for r in rows if r["d"]}

    def recent(self, limit: int = 100) -> list[tuple[Item, str]]:
        """Latest changes, with the title of the source."""
        with self.conn() as c:
            rows = c.execute(
                """SELECT i.*, s.title AS source_title FROM items i JOIN sources s ON s.id = i.source_id
                   WHERE i.status NOT IN ('pending', 'skipped') OR i.error IS NOT NULL
                   ORDER BY i.updated DESC LIMIT ?""",
                (limit,),
            )
            return [(_item(r), r["source_title"]) for r in rows]

    def waiting(self) -> list[Item]:
        """Pending items held back until later (not out yet, throttled)."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE status = 'pending' AND retry_after > ? ORDER BY retry_after", (time.time(),)
            )
            return [_item(r) for r in rows]

    def done_before(self, source_id: int, before: date) -> list[Item]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE source_id = ? AND status = 'done' AND aired < ?",
                (source_id, before.isoformat()),
            )
            return [_item(r) for r in rows]


def _migrate_v1(c: sqlite3.Connection) -> None:
    """nrktv's tables (follows, episodes, seasons by follow) into sources/items."""
    c.executescript(SCHEMA.replace("CREATE TABLE IF NOT EXISTS seasons", "CREATE TABLE IF NOT EXISTS seasons_v2"))
    c.execute(
        """INSERT INTO sources (id, provider, kind, ref, title, plot, series_type, mode, seasons, keep_days, since,
               enabled, image, poster, backdrop, layout, feed_token)
           SELECT id, 'nrk', kind, ref, title, plot, series_type, mode, seasons, keep_days, since, enabled, image,
               poster, backdrop, CASE kind WHEN 'program' THEN 'movie' ELSE 'tv' END, lower(hex(randomblob(16)))
           FROM follows"""
    )
    c.execute(
        """INSERT INTO items (provider, item_id, source_id, season, episode, title, plot, aired, expires, image, year,
               original_title, status, attempts, error, retry_after, files, updated)
           SELECT 'nrk', prf_id, follow_id, season, episode, title, plot, aired, expires, image, year, original_title,
               status, attempts, error, retry_after, files, updated
           FROM episodes"""
    )
    c.execute("INSERT INTO seasons_v2 (source_id, name, scanned) SELECT follow_id, name, scanned FROM seasons")
    c.execute("DROP TABLE seasons")
    c.execute("DROP TABLE episodes")
    c.execute("DROP TABLE follows")
    c.execute("ALTER TABLE seasons_v2 RENAME TO seasons")
