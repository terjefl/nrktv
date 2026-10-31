"""Where things come from. Each provider module has the same functions:

    parse_url(text) -> (kind, ref) | None    a link of theirs, as what to follow
    lookup(kind, ref) -> Info                title, pictures and choices, for the follow form
    search(query) -> list[Hit]
    scan(db, source) -> int                  record new items; returns how many
    ready_at(item) -> datetime | None        None if it can be downloaded now
    video_url(item) -> str                   what yt-dlp is given
    ytdlp_options(cfg, source, item) -> dict format, subtitles, SponsorBlock ...
    page_url(source, item=None) -> str       the item or source on the provider's own site
"""

from dataclasses import dataclass, field
from datetime import date

NAMES = {"nrk": "NRK", "youtube": "YouTube", "adhoc": "Enkelt"}


@dataclass
class Hit:
    provider: str
    kind: str
    ref: str
    title: str
    description: str
    image: str | None


@dataclass
class Choice:
    """A season (or year) the follow form offers."""

    name: str
    label: str


@dataclass
class Info:
    provider: str
    kind: str
    ref: str
    title: str
    plot: str = ""
    image: str | None = None
    poster: str | None = None
    backdrop: str | None = None
    series_type: str = ""
    url: str = ""
    choices: list[Choice] = field(default_factory=list)
    available: bool = True
    expires: date | None = None
    last_aired: date | None = None  # NRK series: the latest episode out, and the next one listed
    next_aired: date | None = None


def get(name: str):
    return PROVIDERS[name]


def parse_url(text: str) -> tuple[str, str, str] | None:
    """(provider, kind, ref) for a link any provider knows."""
    for name, module in PROVIDERS.items():
        if parsed := module.parse_url(text):
            return name, *parsed
    return None


def date_episode(day: date, index: int = 1) -> int:
    """Episode number for something identified by its date: MMDD and a two-digit index,
    22 October -> 102201. YouTube follows Pinchflat, whose index counts down from 99."""
    return day.month * 10000 + day.day * 100 + index


# Last: the provider modules use the classes above
from . import adhoc, nrk, youtube

PROVIDERS = {"nrk": nrk, "youtube": youtube, "adhoc": adhoc}
