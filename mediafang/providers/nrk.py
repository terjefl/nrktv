"""NRK TV: what to fetch for a series or program, and how each episode is numbered.

Sequential series keep NRK's season and episode numbers. Date series (talk shows, news) have
none, so the season is the year and the episode number is made from the date: MMDD plus a
two-digit counter, so 22 October is E102201 and a second broadcast that day E102202. The number
follows from the date alone, whatever order episodes are found in.
"""

import logging
import time
from datetime import date, datetime, timedelta

from ..config import Config
from ..db import DB, Item, Source
from ..titles import strip_number
from . import Choice, Hit, Info, date_episode, nrk_api
from .nrk_api import Instalment, SeasonRef, Series

log = logging.getLogger(__name__)

# Seasons we have looked at are looked at again after this long (new seasons and the newest
# season are looked at every run)
RESCAN_SECONDS = 7 * 24 * 3600

parse_url = nrk_api.parse_url


def ready_at(item: Item) -> datetime | None:
    return nrk_api.ready_at(item.item_id)


def video_url(item: Item) -> str:
    return f"nrk:{item.item_id}"


def page_url(source: Source, item: Item | None = None) -> str:
    if item and source.kind == "series":
        return f"https://tv.nrk.no/serie/{source.ref}/{item.item_id}"
    if source.kind == "series":
        return f"https://tv.nrk.no/serie/{source.ref}"
    return f"https://tv.nrk.no/program/{source.ref}"


def ytdlp_options(cfg: Config, source: Source, item: Item) -> dict:
    # NRK's subtitles are always worth having: they're made for the program, not machine-made
    return {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "subtitleslangs": ["all"],
    }


def search(query: str) -> list[Hit]:
    return [Hit("nrk", h.kind, h.ref, h.title, h.description, h.image) for h in nrk_api.search(query)]


def lookup(kind: str, ref: str) -> Info:
    if kind == "program":
        p = nrk_api.program(ref)
        return Info(
            "nrk", kind, ref, p.title, p.plot, p.image, p.poster, p.backdrop, url=f"https://tv.nrk.no/program/{ref}",
            available=p.available, expires=p.expires,
        )
    s = nrk_api.series(ref)
    last, upcoming = outlook(s)
    if s.dated:
        years = sorted({r.name[:4] for r in s.seasons if r.name[:4].isdigit()}, reverse=True)
        choices = [Choice(y, y) for y in years]
    else:
        choices = [Choice(r.name, r.title) for r in s.seasons]
    return Info(
        "nrk", kind, s.slug, s.title, s.plot, s.image, s.poster, s.backdrop, s.series_type,
        url=f"https://tv.nrk.no/serie/{s.slug}", choices=choices, last_aired=last, next_aired=upcoming,
    )


def outlook(series: Series, fetched: dict[str, list[Instalment]] | None = None) -> tuple[date | None, date | None]:
    """When the latest episode came out, and when the next one NRK lists will. NRK never says a
    series has ended; nothing listed and a long time since the last one is as close as it gets.
    fetched: seasons already looked up, by name."""
    last = upcoming = None
    for ref in newest_seasons(series):
        found = (fetched or {}).get(ref.name)
        for inst in nrk_api.season(series.slug, ref.name) if found is None else found:
            start = inst.rights_from or inst.released
            if inst.coming or (start and start > datetime.now(start.tzinfo)):
                if start and (upcoming is None or start.date() < upcoming):
                    upcoming = start.date()
            elif (day := inst.aired or (inst.released.date() if inst.released else None)) and (last is None or day > last):
                last = day
    return last, upcoming


# ---- what to fetch -------------------------------------------------------------


def newest_seasons(series: Series) -> list[SeasonRef]:
    """The seasons new episodes turn up in: the latest one, for date series the latest two
    (a new month or year starts while the last one may still get its final broadcast)."""
    if not series.seasons:
        return []
    if series.dated:
        return sorted(series.seasons, key=lambda s: s.name, reverse=True)[:2]
    numbered = [s for s in series.seasons if s.name.isdigit()]
    return [max(numbered, key=lambda s: int(s.name))] if numbered else series.seasons[-1:]


def seasons_to_scan(source: Source, series: Series, scanned: dict[str, float], now: float | None = None) -> list[SeasonRef]:
    now = now or time.time()
    newest = {s.name for s in newest_seasons(series)}
    if source.mode == "new":
        refs = [s for s in series.seasons if s.name in newest]
    else:
        refs = [s for s in series.seasons if source.wants_season(s.name)]
    if source.dated and source.keep_days:
        cutoff = date.today() - timedelta(days=source.keep_days)
        refs = [s for s in refs if not _period_ends_before(s.name, cutoff)]
    return [s for s in refs if s.name in newest or s.name not in scanned or now - scanned[s.name] > RESCAN_SECONDS]


def _period_ends_before(name: str, cutoff: date) -> bool:
    """Whether a date series' season ("2024" or "202610") is over before cutoff."""
    if not name.isdigit():
        return False
    if len(name) == 4:
        return date(int(name), 12, 31) < cutoff
    if len(name) == 6:
        year, month = int(name[:4]), int(name[4:])
        if not 1 <= month <= 12:
            return False
        first_next = date(year + month // 12, month % 12 + 1, 1)
        return first_next - timedelta(days=1) < cutoff
    return False


def wanted(source: Source, inst: Instalment) -> bool:
    """Whether an episode NRK lists should be downloaded."""
    if not inst.available:
        return False  # not out yet, or gone
    if inst.rights_from and inst.rights_from > datetime.now(inst.rights_from.tzinfo):
        return False  # tonight's broadcast: listed before it can be watched; a later run takes it
    if source.mode == "new":
        released = inst.released.timestamp() if inst.released else None
        if released is None or released < source.since.timestamp():
            return False
    if source.dated and source.keep_days and inst.aired:
        if inst.aired < date.today() - timedelta(days=source.keep_days):
            return False
    return True


def season_number(series: Series, ref: SeasonRef) -> int:
    if ref.name.isdigit() and not series.dated:
        return int(ref.name)
    names = [s.name for s in series.seasons]
    return names.index(ref.name) + 1 if ref.name in names else 1


def plan(source: Source, series: Series, ref: SeasonRef, position: int, inst: Instalment) -> Item:
    """The library row for an episode. position: its 0-based place in the season's list."""
    if series.dated:
        aired = inst.aired or (inst.released.date() if inst.released else date.today())
        season, episode, title = aired.year, date_episode(aired), inst.title
    else:
        aired = inst.aired
        season = season_number(series, ref)
        episode = inst.sequence or position + 1
        title = strip_number(inst.title)
    return Item(
        provider="nrk", item_id=inst.prf_id, source_id=source.id, season=season, episode=episode, title=title,
        plot=inst.plot, aired=aired, expires=inst.expires, image=inst.image, year=inst.year,
        original_title=inst.original_title,
    )


def scan(db: DB, source: Source, cfg: Config | None = None) -> int:
    """Look up what NRK has for a source and record new episodes. Returns how many."""
    if source.kind == "program":
        if (known := db.item("nrk", source.ref)) and known.status == "done":
            return 0  # nothing more will come of it
        p = nrk_api.program(source.ref)
        db.update_meta(source.id, plot=p.plot, image=p.image, poster=p.poster, backdrop=p.backdrop)
        if not p.available:
            return 0
        item = Item(
            provider="nrk", item_id=p.prf_id, source_id=source.id, season=0, episode=0, title=p.title, plot=p.plot,
            aired=p.aired, expires=p.expires, image=p.image, year=p.year, original_title=p.original_title,
        )
        return int(db.add_item(item))

    series = nrk_api.series(source.ref)
    db.update_meta(
        source.id, plot=series.plot, series_type=series.series_type,
        image=series.image, poster=series.poster, backdrop=series.backdrop,
    )
    source = db.source(source.id)
    new = 0
    fetched: dict[str, list[Instalment]] = {}
    for ref in seasons_to_scan(source, series, db.scanned(source.id)):
        fetched[ref.name] = nrk_api.season(series.slug, ref.name)
        for position, inst in enumerate(fetched[ref.name]):
            if wanted(source, inst):
                new += db.add_item(plan(source, series, ref, position, inst), unique_number=series.dated)
        db.mark_scanned(source.id, ref.name)
    last, upcoming = outlook(series, fetched)
    db.update_meta(source.id, last_aired=last, next_aired=upcoming)
    if new:
        log.info("%s: %d new episode(s)", source.title, new)
    return new
