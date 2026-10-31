"""NRK's catalogue API (psapi.nrk.no): search, series, seasons and programs.

Undocumented but stable, and the same API tv.nrk.no and yt-dlp use. Series come in three types:
`sequential` (real seasons and episode numbers), `standard` (seasons are years) and `news`
(seasons are months); the last two have no episode numbers, only dates.
"""

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

BASE = "https://psapi.nrk.no"
USER_AGENT = "nrktv/0.1 (+https://github.com/terjefl/nrktv)"
# Rights that never end are given as 2099/2100
FOREVER = datetime(2099, 1, 1).date()


@dataclass
class SearchHit:
    kind: str  # "series" or "program"
    ref: str  # series slug or program id
    title: str
    description: str
    image: str | None


@dataclass
class SeasonRef:
    name: str  # "1", "2024", "202610"
    title: str  # "Sesong 1", "2024", "Oktober 2026"


@dataclass
class Series:
    slug: str
    title: str
    plot: str
    series_type: str  # sequential | standard | news
    image: str | None  # 16:9
    poster: str | None  # 2:3, not every series has one
    backdrop: str | None
    seasons: list[SeasonRef] = field(default_factory=list)

    @property
    def dated(self) -> bool:
        return self.series_type != "sequential"


@dataclass
class Instalment:
    """An episode in a season, or a single program."""

    prf_id: str
    title: str
    plot: str
    sequence: int | None  # episode number in sequential series
    aired: date | None
    released: datetime | None  # on tv.nrk.no
    expires: date | None
    available: bool
    geo_blocked: bool
    image: str | None
    year: int | None = None
    original_title: str | None = None
    # When it can be watched on demand; NRK lists tonight's broadcast as "available" before that
    rights_from: datetime | None = None
    coming: bool = False  # NRK lists it ahead of time ("Kommer om 6 dager")


@dataclass
class Program(Instalment):
    poster: str | None = None
    backdrop: str | None = None


class NotFound(Exception):
    pass


def get(path: str) -> dict:
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(path) from e
        raise


def best_image(images: list | None) -> str | None:
    """The widest image URL in an NRK image list ([{url, width}] or [{imageUrl, pixelWidth}])."""
    best = None
    for img in images or []:
        url = img.get("url") or img.get("imageUrl")
        width = img.get("width") or img.get("pixelWidth") or 0
        if url and (best is None or width > best[0]):
            best = (width, url)
    return best[1] if best else None


def search(query: str) -> list[SearchHit]:
    data = get("/search?" + urllib.parse.urlencode({"q": query}))
    hits = []
    for h in data.get("hits") or []:
        kind = {"serie": "series", "program": "program"}.get(h.get("type"))
        hit = h.get("hit") or {}
        if not kind or not hit.get("id"):
            continue
        hits.append(
            SearchHit(
                kind=kind,
                ref=hit["id"],
                title=hit.get("title", ""),
                description=hit.get("description") or "",
                image=best_image((hit.get("image") or {}).get("webImages")),
            )
        )
    return hits


def series(slug: str) -> Series:
    data = get(f"/tv/catalog/series/{urllib.parse.quote(slug)}")
    kind = data["seriesType"]
    s = data[kind]
    titles = s.get("titles") or {}
    return Series(
        slug=s.get("urlFriendlySeriesId") or slug,
        title=titles.get("title", slug),
        plot=titles.get("subtitle") or "",
        series_type=kind,
        image=best_image(s.get("image")),
        poster=best_image(s.get("posterImage")),
        backdrop=best_image(s.get("backdropImage")),
        seasons=[SeasonRef(x["name"], x.get("title") or x["name"]) for x in data["_links"].get("seasons") or []],
    )


def season(slug: str, name: str) -> list[Instalment]:
    """Every episode in a season, following NRK's paging."""
    path = f"/tv/catalog/series/{urllib.parse.quote(slug)}/seasons/{urllib.parse.quote(name)}"
    result: list[Instalment] = []
    while path:
        data = get(path)
        emb = data.get("_embedded") or {}
        key = "episodes" if "episodes" in emb else "instalments"
        items = emb.get(key) or []
        next_link = (data.get("_links") or {}).get("next")
        if isinstance(items, dict):  # paged: {_embedded: {episodes: [...]}, _links: {next}}
            next_link = next_link or (items.get("_links") or {}).get("next")
            items = (items.get("_embedded") or {}).get(key) or []
        result += [_instalment(e) for e in items]
        path = next_link["href"] if next_link else None
    return result


def program(prf_id: str) -> Program:
    data = get(f"/tv/catalog/programs/{urllib.parse.quote(prf_id)}")
    info = data.get("programInformation") or {}
    more = data.get("moreInformation") or {}
    rights = more.get("usageRights") or {}
    titles = info.get("titles") or {}
    released = _datetime(more.get("releaseDateOnDemand"))
    return Program(
        prf_id=prf_id,
        title=titles.get("title", prf_id),
        plot=titles.get("subtitle") or "",
        sequence=None,
        aired=released.date() if released else None,
        released=released,
        expires=_expires(rights),
        available=(info.get("availability") or {}).get("status") == "available",
        geo_blocked=bool((rights.get("geoBlock") or {}).get("isGeoBlocked")),
        image=best_image(info.get("image")),
        year=more.get("productionYear"),
        original_title=more.get("originalTitle"),
        rights_from=_datetime((rights.get("from") or {}).get("date")),
        poster=best_image(data.get("posterImage")),
        backdrop=best_image(data.get("backdropImage")),
    )


def ready_at(prf_id: str) -> datetime | None:
    """None if a program can be watched on demand now, else when it will be: still on air, or
    on demand from a later time. NRK's catalogue calls tonight's broadcast "available" while it
    is on air, but only the player's manifest says whether there is anything to download yet."""
    try:
        manifest = get(f"/playback/manifest/program/{urllib.parse.quote(prf_id)}")
    except Exception:
        return None  # let the download say what is wrong
    avail = manifest.get("availability") or {}
    now = datetime.now().astimezone()
    on_demand = avail.get("onDemand") or {}
    if on_demand.get("hasRightsNow"):
        # Right after a broadcast NRK plays it back from the live stream (hosts like
        # nrk-live-world... or ...-liveedge1...) until the recording is published on its
        # on-demand hosts (nrk-od-..., ...-odedge1...); yt-dlp finds no formats in the former
        hosts = [urllib.parse.urlparse(a.get("url", "")).hostname or "" for a in
                 (manifest.get("playable") or {}).get("assets") or []]
        if hosts and all(any(part.startswith("live") for part in re.split(r"[.-]", h)) for h in hosts):
            return now + timedelta(minutes=30)
        return None
    start = _datetime(on_demand.get("from"))
    if start and start > now:
        return start
    live = avail.get("live") or {}
    end = _datetime((live.get("transmissionInterval") or {}).get("to"))
    if live.get("isOngoing") or (end and end > now):
        # NRK publishes the recording a little after the broadcast ends
        return max(end, now) + timedelta(minutes=15) if end else now + timedelta(minutes=30)
    return None


def _instalment(e: dict) -> Instalment:
    rights = e.get("usageRights") or {}
    titles = e.get("titles") or {}
    released = _datetime(e.get("releaseDateOnDemand"))
    # News and talk shows give the broadcast date ("22.10.2026"); otherwise the date it went
    # online is the best there is (for archive series NRK sets it to the original air date)
    aired = _ddmmyyyy(e.get("firstTransmissionDateDisplayValue")) or (released.date() if released else None)
    return Instalment(
        prf_id=e["prfId"],
        title=titles.get("title") or "",
        plot=titles.get("subtitle") or "",
        sequence=e.get("sequenceNumber"),
        aired=aired,
        released=released,
        expires=_expires(rights),
        available=(e.get("availability") or {}).get("status") == "available",
        geo_blocked=bool((rights.get("geoBlock") or {}).get("isGeoBlocked")),
        image=best_image(e.get("image")),
        year=e.get("productionYear"),
        original_title=e.get("originalTitle"),
        rights_from=_datetime((rights.get("from") or {}).get("date")),
        coming=(e.get("availability") or {}).get("status") == "coming",
    )


def _datetime(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _ddmmyyyy(value: str | None) -> date | None:
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", (value or "").strip())
    if not m:
        return None
    try:
        return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None


def _expires(rights: dict) -> date | None:
    to = _datetime((rights.get("to") or {}).get("date"))
    if not to:
        return None
    return None if to.date() >= FOREVER else to.date()


# tv.nrk.no/serie/<slug>[/sesong/..][/episode/..], tv.nrk.no/serie/<slug>/<prfId>,
# tv.nrk.no/program/<prfId>, tv.nrk.no/se?v=<prfId>, or a bare program id
_SERIES_URL = re.compile(r"tv\.nrk\.no/serie/([\w-]+)", re.IGNORECASE)
_PROGRAM_URL = re.compile(r"tv\.nrk\.no/(?:program/|se\?v=)([A-Z]{4}\d{8})", re.IGNORECASE)
_PRF_ID = re.compile(r"[A-Z]{4}\d{8}")


def parse_url(text: str) -> tuple[str, str] | None:
    """("series", slug) or ("program", prf_id) for a tv.nrk.no link, or None."""
    text = text.strip()
    if m := _SERIES_URL.search(text):
        return "series", m[1].lower()
    if m := _PROGRAM_URL.search(text):
        return "program", m[1].upper()
    if _PRF_ID.fullmatch(text.upper()):
        return "program", text.upper()
    return None
