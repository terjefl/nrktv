"""The web page: find something on NRK TV or YouTube, choose what to follow and how, and see how
the downloads are going.

No login of its own; it sits behind Cloudflare Access like the other internal pages. Only the
podcast feeds (/feed/<token>...) are meant to be reachable without it.
"""

import html
import json
import logging
import re
import threading
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import favicon, feeds, providers
from .config import Config
from .db import DB, MAX_ATTEMPTS, Item, Source
from .harvest import Progress
from .library import DEFAULT_LIBRARY, LAYOUTS
from .providers import adhoc

log = logging.getLogger(__name__)

MODES = {"all": "Alt som finnes", "seasons": "Valgte sesonger", "new": "Bare nye"}
STATUS = {
    "pending": "Venter",
    "done": "Lastet ned",
    "failed": "Feilet",
    "expired": "Utløpt hos NRK",
    "geo": "Bare i Norge",
    "unavailable": "Ikke tilgjengelig",
    "skipped": "Hoppet over",
    "removed": "Ryddet bort",
}
TYPES = {"sequential": "Serie", "standard": "Datoserie", "news": "Nyheter"}
QUALITIES = {2160: "Inntil 4K", 1440: "Inntil 1440p", 1080: "Inntil 1080p", 720: "Inntil 720p", 0: "Bare lyd"}
SB_CATEGORIES = {
    "sponsor": "Sponsor", "selfpromo": "Egenreklame", "interaction": "«Abonner»-mas", "intro": "Intro",
    "outro": "Outro", "preview": "Forhåndsvisning", "music_offtopic": "Musikk uten innhold", "filler": "Utfylling",
}
PRESETS = {
    "tv": ("Serie (TV)", "Hver kanal blir en serie: sesong per år, episoder etter dato, med plakat og beskrivelser."),
    "videos": ("Videoer", "En mappe per video, som filmbibliotek. Passer for DJ-sett, konserter og foredrag."),
    "audio": ("Bare lyd + podkast", "Bare lydsporet, og en podkast-feed du kan legge inn i podkastappen."),
}
# Manual and migration reasons for skipping; filter reasons are recomputed when settings change
STICKY_SKIPS = ("hoppet over manuelt", "fjernet i Pinchflat", "fil mangler")

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--text:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--accent:#0b5cad;
--ok:#1e7d3a;--warn:#a35d00;--bad:#b3261e;--chip:#eeece7;--nrk:#0b5cad;--yt:#c4302b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121214;--card:#1c1c1f;--text:#ececef;
--muted:#9a9aa2;--line:#2c2c31;--accent:#6aa8ff;--ok:#5cc27a;--warn:#e0a14a;--bad:#ff7a70;--chip:#26262b;
--nrk:#6aa8ff;--yt:#ff6b63}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:14px 16px;
border-bottom:1px solid var(--line);background:var(--card)}
header h1{font-size:18px;margin:0}header h1 a{color:var(--text)}
nav{display:flex;gap:14px;font-size:14px}
form.search{display:flex;gap:6px;flex:1 1 320px;max-width:560px}
input[type=text],input[type=search],input[type=number],input[type=date],select{font:inherit;padding:7px 10px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);min-width:0}
form.search input{flex:1}
button,.btn{font:inherit;font-size:14px;padding:7px 12px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--text);cursor:pointer;white-space:nowrap;display:inline-block}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad)}
main{max-width:1100px;margin:0 auto;padding:18px 16px 48px}
h2{font-size:17px;margin:22px 0 10px}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.card img,.card .ph{width:100%;aspect-ratio:16/9;object-fit:cover;background:var(--chip);display:block}
.ph{display:flex!important;align-items:center;justify-content:center;font-size:42px;font-weight:700;color:var(--muted);
background:linear-gradient(135deg,var(--chip),var(--line))!important}
.hero .ph{width:280px;max-width:100%;border-radius:10px;aspect-ratio:16/9}
.card .body{padding:10px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
.card h3{font-size:15px;margin:0}
.muted{color:var(--muted);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--chip);color:var(--muted)}
.chip.ok{color:var(--ok)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}
.chip.nrk{color:var(--nrk);font-weight:600}.chip.youtube{color:var(--yt);font-weight:600}
a.chip.on{background:var(--accent);color:#fff}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:auto;padding-top:6px}
.actions form{margin:0}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}
td.code{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--muted)}
.tablewrap{overflow-x:auto}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:12px}
.hero{display:flex;gap:16px;flex-wrap:wrap}.hero img{width:280px;max-width:100%;border-radius:10px;aspect-ratio:16/9;object-fit:cover}
.hero .text{flex:1 1 280px}
fieldset{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0;display:flex;flex-direction:column;gap:6px}
legend{font-size:13px;color:var(--muted);padding:0 4px}
label.opt{display:flex;gap:8px;align-items:baseline;padding:2px 0}
label.field{display:flex;flex-direction:column;gap:3px;font-size:13px;color:var(--muted)}
.fields{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.seasons{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:2px 12px;margin-top:6px}
.empty{padding:28px;text-align:center;color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:12px}
.flash{padding:10px 12px;border-radius:10px;background:var(--chip);margin-bottom:12px}
.queue{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;display:flex;
flex-direction:column;gap:8px;margin-bottom:8px}
.bar{height:6px;border-radius:99px;background:var(--chip);overflow:hidden}.bar span{display:block;height:100%;background:var(--accent)}
details summary{cursor:pointer;color:var(--muted);font-size:13px}
"""


FETCH_FORM = """<form method="post" action="/hent" class="queue" style="flex-direction:row;flex-wrap:wrap;align-items:center">
<strong>Hent én video</strong><input type="text" name="url" placeholder="Lim inn en lenke (YouTube, Vimeo, NRK …)"
 style="flex:1 1 260px" required><button class="primary" name="type" value="video">Video</button>
<button name="type" value="audio">Bare lyd</button></form>"""


def picture(url: str | None, title: str) -> str:
    """The picture, or (when there is none, or it doesn't load) a plain tile with the initial."""
    initial = _e((title or "?").strip()[:1].upper())
    tile = f'<div class="ph">{initial}</div>'
    if not url:
        return tile
    return (f'<img src="{_e(url)}" alt="" loading="lazy" referrerpolicy="no-referrer" '
            f"""onerror="this.replaceWith(Object.assign(document.createElement('div'),{{className:'ph',textContent:'{initial}'}}))">""")


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _size(n: int) -> str:
    if n >= 1e12:
        return f"{n / 1e12:.1f} TB"
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.0f} MB" if n else ""


def page(title: str, body: str, query: str = "") -> bytes:
    doc = f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)} · Mediafang</title>{favicon.LINKS}
<style>{CSS}</style></head><body>
<header><h1><a href="/">Mediafang</a></h1>
<form class="search" action="/sok"><input type="search" name="q" value="{_e(query)}"
 placeholder="Søk i NRK TV, eller lim inn en lenke fra NRK eller YouTube" aria-label="Søk"><button>Søk</button></form>
<nav><a href="/">Følger</a><a href="/logg">Logg</a></nav></header>
<main>{body}</main></body></html>"""
    return doc.encode()


def code(source: Source, item: Item) -> str:
    if source.layout == "movie":
        return "Film"
    if source.provider == "youtube":
        return _date(item.aired)
    if source.dated:
        return f"{item.season} #{item.episode}"
    return f"S{item.season:02d}E{item.episode:02d}"


# NRK never says a series has ended: with nothing listed, this long since the last episode is the sign.
# Talk shows and news take a summer break; drama can go a year or two between seasons
ENDED_AFTER_DAYS = {True: 183, False: 730}


def is_program(source: Source) -> bool:
    """A single NRK program or film: downloaded once, not followed."""
    return source.provider == "nrk" and source.kind == "program"


def outlook_chip(series_type: str, last: date | None, upcoming: date | None) -> str:
    """What NRK has coming for a series, or that nothing has come for a long time."""
    if upcoming:
        return f'<span class="chip ok">ny episode {upcoming.strftime("%d.%m")}</span>'
    if not last:
        return ""
    if (date.today() - last).days > ENDED_AFTER_DAYS[series_type != "sequential"]:
        return f'<span class="chip" title="NRK har ingen nye episoder på listen">trolig avsluttet · siste {_date(last)}</span>'
    return f'<span class="chip">siste episode {_date(last)}</span>'


def program_status(counts: dict[str, int]) -> tuple[str, str]:
    """(label, chip class) for a single program's one item."""
    for status, cls in (("done", "ok"), ("failed", "bad"), ("pending", "")):
        if counts.get(status):
            return STATUS[status] if status != "pending" else "I kø", cls
    status = next(iter(counts), "")
    return (STATUS.get(status, status), "warn") if status else ("Sjekkes", "")


def kind_label(source: Source) -> str:
    if source.provider in ("youtube", "adhoc"):
        return LAYOUTS.get(source.layout, source.layout)
    if source.kind == "program":
        return "Film"
    return TYPES.get(source.series_type, "Serie")


def mode_text(source: Source) -> str:
    if source.kind == "program":
        return "Enkeltprogram, lastes ned én gang"
    if source.provider == "adhoc":
        where = f"{source.library}/{source.folder}" if source.folder else f"{source.library}/<kanal>"
        return f"Det du henter selv · {where}"
    text = MODES.get(source.mode, source.mode)
    if source.mode == "seasons":
        text += ": " + ", ".join(source.seasons)
    if source.keep_days:
        text += f" · beholder {source.keep_days} dager"
    if source.provider == "youtube":
        text += f" · {source.library or DEFAULT_LIBRARY.get(source.layout, '')}/{source.folder_name}"
    return text


class App:
    def __init__(self, cfg: Config, db: DB, wake: threading.Event | None = None, progress: Progress | None = None):
        self.cfg = cfg
        self.db = db
        self.wake = wake or threading.Event()
        self.progress = progress or Progress()

    # ---- pages -----------------------------------------------------------------

    def index(self, flash: str = "", provider: str = "") -> bytes:
        sources = self.db.sources(provider=provider or None)
        counts, sizes, expiry = self.db.counts(), self.db.sizes(), self.db.next_expiry()
        body = f'<div class="flash">{_e(flash)}</div>' if flash else ""
        body += self._queue()
        chips = "".join(
            f'<a class="chip{" on" if provider == p else ""}" href="/{"?p=" + p if p else ""}">{label}</a>'
            for p, label in (("", "Alle"), ("nrk", "NRK"), ("youtube", "YouTube"), ("adhoc", "Enkelt"))
        )
        body += FETCH_FORM
        body += f"""<div class="row"><h2>Det du følger</h2><div class="chips">{chips}</div>
<form method="post" action="/kjor"><button>Sjekk nå</button></form></div>"""
        if not sources:
            body += '<div class="empty">Ingenting her ennå. Søk etter en serie, eller lim inn en lenke over.</div>'
            return page("Følger", body)
        programs = [s for s in sources if is_program(s)]
        cards = []
        for s in sources:
            if is_program(s):
                continue
            c = counts.get(s.id, {})
            chips = [f'<span class="chip {s.provider}">{providers.NAMES[s.provider]}</span>',
                     f'<span class="chip">{_e(kind_label(s))}</span>']
            if c.get("done"):
                chips.append(f'<span class="chip ok">{c["done"]} lastet ned</span>')
            if c.get("pending"):
                chips.append(f'<span class="chip">{c["pending"]} venter</span>')
            if c.get("failed"):
                chips.append(f'<span class="chip bad">{c["failed"]} feilet</span>')
            if sizes.get(s.id):
                chips.append(f'<span class="chip">{_size(sizes[s.id])}</span>')
            if s.id in expiry:
                chips.append(f'<span class="chip warn">utløper {_date(expiry[s.id])}</span>')
            if s.provider == "nrk":
                chips.append(outlook_chip(s.series_type, s.last_aired, s.next_aired))
            if not s.enabled:
                chips.append('<span class="chip warn">pauset</span>')
            toggle = "Fortsett" if not s.enabled else "Pause"
            edit = unfollow = ""
            if s.provider != "adhoc":  # the one-off sources are fixed
                edit = f'<a class="btn" href="{self._edit_link(s)}">Endre</a>'
                unfollow = f"""<form method="post" action="/slett" onsubmit="return confirm('Slutte å følge {_e(s.folder_name)}? Filene blir liggende.')">
<input type="hidden" name="id" value="{s.id}"><button class="danger">Slutt å følge</button></form>"""
            cards.append(f"""<div class="card"><a href="/kilde/{s.id}">{picture(s.image or s.poster, s.folder_name)}</a>
<div class="body"><h3><a href="/kilde/{s.id}">{_e(s.folder_name)}</a></h3>
<div class="muted">{_e(mode_text(s))}</div><div class="chips">{"".join(chips)}</div>
<div class="actions">{edit}
<form method="post" action="/pause"><input type="hidden" name="id" value="{s.id}"><button>{toggle}</button></form>
{unfollow}</div></div></div>""")
        if cards:
            body += f'<div class="grid">{"".join(cards)}</div>'
        if programs:
            body += self._programs(programs, counts, sizes, expiry, heading=bool(cards))
        return page("Følger", body)

    def _programs(self, programs: list[Source], counts, sizes, expiry, heading: bool) -> str:
        """Single NRK programs and films: a list of what was fetched, not cards to follow."""
        rows = []
        for s in programs:
            label, cls = program_status(counts.get(s.id, {}))
            rows.append(f"""<tr><td><a href="/kilde/{s.id}">{_e(s.folder_name)}</a></td>
<td><span class="chip {cls}">{_e(label)}</span></td><td class="code">{_size(sizes.get(s.id, 0))}</td>
<td class="code">{_date(expiry.get(s.id))}</td><td style="white-space:nowrap">
<form method="post" action="/slett" style="margin:0" onsubmit="return confirm('Fjerne {_e(s.folder_name)} fra listen? Filen blir liggende.')">
<input type="hidden" name="id" value="{s.id}"><button class="danger">Fjern</button></form></td></tr>""")
        head = '<h2>Filmer og enkeltprogrammer</h2>' if heading else ""
        return (head + '<div class="tablewrap"><table><tr><th>Tittel</th><th>Status</th><th>Størrelse</th><th>Utløper</th>'
                '<th></th></tr>' + "".join(rows) + "</table></div>")

    def _queue(self) -> str:
        p = self.progress
        due = self.db.due()
        waiting = self.db.waiting()
        if not (p.label or due or waiting):
            return ""
        parts = []
        if p.label:
            parts.append(f"""<div><strong>Laster ned nå:</strong> {_e(p.label)}
<span class="muted">{p.percent:.0f}% {_e(p.speed)}</span></div><div class="bar"><span style="width:{p.percent:.0f}%"></span></div>""")
        if due:
            names = ", ".join(_e(i.title) for i in due[:5]) + (" …" if len(due) > 5 else "")
            parts.append(f'<div><strong>I kø:</strong> {len(due)} <span class="muted">{names}</span></div>')
        if waiting:
            nxt = datetime.fromtimestamp(waiting[0].retry_after).strftime("%d.%m %H:%M")
            parts.append(f'<div><strong>Venter:</strong> {len(waiting)} <span class="muted">neste forsøk {nxt}</span></div>')
        refresh = '<meta http-equiv="refresh" content="15">' if p.label else ""
        return f'<div class="queue">{refresh}{"".join(parts)}</div>'

    def _edit_link(self, s: Source) -> str:
        return f"/folg?provider={s.provider}&kind={s.kind}&ref={urllib.parse.quote(s.ref)}"

    def fetch_page(self, url: str) -> bytes:
        """A link nothing can follow (a single video): offer to download it."""
        body = f"""<div class="panel"><h2 style="margin:0">Hent denne</h2><div class="muted">{_e(url)}</div>
<form method="post" action="/hent" class="row" style="justify-content:flex-start">
<input type="hidden" name="url" value="{_e(url)}">
<button class="primary" name="type" value="video">Video</button><button name="type" value="audio">Bare lyd</button></form>
<div class="muted">Video havner i biblioteket «Enkeltvideoer», lyd i «Enkeltlyd» med egen podkast-feed.</div></div>"""
        return page("Hent", body, url)

    def fetch(self, url: str, audio: bool) -> str:
        """Queue a one-off download and start the loop. Returns a message for the user."""
        url = url.strip()
        if not re.match(r"https?://", url):
            raise ValueError("Ikke en lenke")
        result = adhoc.add(self.db, url, audio=audio)
        self.wake.set()
        return result.message

    def search(self, q: str, youtube: bool = False) -> bytes:
        q = q.strip()
        if re.match(r"https?://", q):
            return self.fetch_page(q)
        if not q:
            return page("Søk", '<div class="empty">Skriv noe å søke etter.</div>')
        try:
            hits = providers.get("youtube" if youtube else "nrk").search(q)
        except Exception as e:
            return page("Søk", f'<div class="empty">Søket feilet: {_e(e)}</div>', q)
        followed = {(s.provider, s.ref): s for s in self.db.sources()}
        counts = self.db.counts()
        where = "YouTube" if youtube else "NRK TV"
        other = (f'<a class="btn" href="/sok?q={urllib.parse.quote(q)}">Søk i NRK TV i stedet</a>' if youtube else
                 f'<a class="btn" href="/sok?q={urllib.parse.quote(q)}&yt=1">Søk etter YouTube-kanaler</a>')
        head = f'<div class="row"><h2>{where}: «{_e(q)}»</h2>{other}</div>'
        if not hits:
            return page("Søk", head + '<div class="empty">Ingen treff.</div>', q)
        cards = []
        for h in hits:
            kind = {"series": "Serie", "program": "Film / program", "channel": "Kanal", "playlist": "Spilleliste"}[h.kind]
            link = f"/folg?provider={h.provider}&kind={h.kind}&ref={urllib.parse.quote(h.ref)}"
            if (have := followed.get((h.provider, h.ref))) and is_program(have):
                label, cls = program_status(counts.get(have.id, {}))
                action = f'<a class="chip {cls}" href="/kilde/{have.id}">{_e(label)}</a>'
            elif have:
                action = f'<a class="chip ok" href="/kilde/{have.id}">Følger</a>'
            elif h.provider == "nrk" and h.kind == "program":
                action = f'<a class="btn" href="{link}">Last ned …</a>'
            else:
                action = f'<a class="btn" href="{link}">Følg …</a>'
            cards.append(f"""<div class="card">{picture(h.image, h.title)}<div class="body">
<h3>{_e(h.title)}</h3><div class="chips"><span class="chip {h.provider}">{providers.NAMES[h.provider]}</span>
<span class="chip">{kind}</span></div><div class="muted">{_e(h.description[:220])}</div>
<div class="actions">{action}</div></div></div>""")
        return page("Søk", head + f'<div class="grid">{"".join(cards)}</div>', q)

    def follow_form(self, provider: str, kind: str, ref: str) -> bytes:
        try:
            info = providers.get(provider).lookup(kind, ref)
        except Exception as e:
            return page("Finnes ikke", f'<div class="empty">Fant ikke dette: {_e(e)}</div>')
        existing = self.db.source_by_ref(provider, info.ref)
        if provider == "youtube":
            options = self._youtube_options(info, existing)
        elif kind == "program":
            options = "" if info.available else '<div class="flash">NRK viser ikke dette programmet nå.</div>'
            if info.expires:
                options += f'<div class="muted">Tilgjengelig hos NRK til {_date(info.expires)}.</div>'
            options += '<div class="muted">Lastes ned én gang til filmbiblioteket, uten å følges videre.</div>'
        else:
            options = self._nrk_series_options(info, existing)
        verb = "Lagre" if existing else "Følg"
        if provider == "nrk" and kind == "program":
            verb = "Last ned"
            if existing:  # nothing to choose for a single program: say how it went
                label, cls = program_status(self.db.counts().get(existing.id, {}))
                return page(info.title, f"""<div class="panel"><div class="hero">{picture(info.image or info.poster, info.title)}
<div class="text"><h2 style="margin-top:0">{_e(info.title)}</h2><div class="muted">{_e(info.plot[:600])}</div>
<p><span class="chip {cls}">{_e(label)}</span></p><a class="btn" href="/kilde/{existing.id}">Vis</a></div></div></div>""")
        body = f"""<form method="post" action="/folg" class="panel">
<input type="hidden" name="provider" value="{_e(provider)}"><input type="hidden" name="kind" value="{_e(info.kind)}">
<input type="hidden" name="ref" value="{_e(info.ref)}">
<div class="hero">{picture(info.image or info.poster, info.title)}<div class="text">
<h2 style="margin-top:0">{_e(info.title)}</h2><div class="muted">{_e(info.plot[:600])}</div></div></div>{options}
<div><button class="primary">{verb}</button> <a class="btn" href="/">Avbryt</a></div></form>"""
        return page(info.title, body)

    def _nrk_series_options(self, info: providers.Info, existing: Source | None) -> str:
        dated = info.series_type != "sequential"
        # News goes back decades (Dagsrevyen: 600 months); take only what comes unless asked
        mode = existing.mode if existing else ("new" if info.series_type == "news" else "all")
        chosen = set(existing.seasons if existing else [])
        boxes = "".join(
            f'<label class="opt"><input type="checkbox" name="season" value="{_e(c.name)}"'
            f'{" checked" if c.name in chosen else ""}> {_e(c.label)}</label>'
            for c in info.choices
        )
        modes = {**MODES, "new": "Bare nye episoder"}
        radios = "".join(
            f'<label class="opt"><input type="radio" name="mode" value="{m}"{" checked" if m == mode else ""}> {label}</label>'
            for m, label in modes.items()
        )
        keep = ""
        if dated:
            days = existing.keep_days if existing and existing.keep_days else ""
            keep = f"""<fieldset><legend>Opprydding</legend><label class="opt">Behold de siste
<input type="number" name="keep_days" min="1" value="{days}" style="width:90px"> dager (tomt = behold alt)</label>
<div class="muted">Eldre sendinger slettes fra biblioteket, og bare sendinger innenfor perioden lastes ned.</div></fieldset>"""
        if info.series_type == "news":
            keep += '<div class="muted">Nyhetsserier har ofte tusenvis av sendinger; «Alt som finnes» henter alle.</div>'
        what = "år" if dated else "sesonger"
        return f"""<div class="chips"><span class="chip nrk">NRK</span><span class="chip">{TYPES.get(info.series_type, "Serie")}</span>
<span class="chip">{len(info.choices)} {what}</span>{outlook_chip(info.series_type, info.last_aired, info.next_aired)}</div>
<fieldset><legend>Hva skal lastes ned?</legend>{radios}
<div class="muted">Velg {what} under hvis du velger «{MODES["seasons"]}».</div>
<div class="seasons">{boxes}</div></fieldset>{keep}"""

    def _youtube_options(self, info: providers.Info, s: Source | None) -> str:
        s = s or Source(id=0, provider="youtube", kind=info.kind, ref=info.ref, title=info.title)
        libraries = sorted({x.library for x in self.db.sources(provider="youtube") if x.library} | set(DEFAULT_LIBRARY.values()))
        presets = "".join(
            f'<label class="opt"><input type="radio" name="layout" value="{k}"{" checked" if s.layout == k else ""}>'
            f' <span><strong>{label}</strong> <span class="muted">{_e(text)}</span></span></label>'
            for k, (label, text) in PRESETS.items()
        )
        qualities = "".join(
            f'<option value="{q}"{" selected" if s.quality == q else ""}>{label}</option>' for q, label in QUALITIES.items()
        )
        sb = "".join(
            f'<option value="{v}"{" selected" if s.sponsorblock == v else ""}>{label}</option>'
            for v, label in (("", "Av"), ("mark", "Marker som kapitler"), ("remove", "Klipp bort"))
        )
        cats = set(s.sb_categories.split(","))
        sb_boxes = "".join(
            f'<label class="opt"><input type="checkbox" name="sb_category" value="{k}"{" checked" if k in cats else ""}> {label}</label>'
            for k, label in SB_CATEGORIES.items()
        )
        libs = "".join(f'<option value="{_e(x)}">' for x in libraries)
        mins = lambda v: round(v / 60) if v else ""
        return f"""<div class="chips"><span class="chip youtube">YouTube</span><span class="chip">{"Kanal" if info.kind == "channel" else "Spilleliste"}</span></div>
<fieldset><legend>Hvordan skal det ligge i Plex og Jellyfin?</legend>{presets}</fieldset>
<fieldset><legend>Plassering</legend><div class="fields">
<label class="field">Bibliotekmappe (ett bibliotek i Plex/Jellyfin)<input type="text" name="library" list="libs"
 value="{_e(s.library)}" placeholder="TV, Video eller Lyd etter valget over"><datalist id="libs">{libs}</datalist></label>
<label class="field">Mappenavn for kanalen<input type="text" name="folder" value="{_e(s.folder)}" placeholder="{_e(info.title)}"></label>
<label class="field">Kvalitet<select name="quality">{qualities}</select></label></div></fieldset>
<fieldset><legend>Hva skal lastes ned?</legend>
<label class="opt"><input type="radio" name="mode" value="all"{" checked" if s.mode != "new" else ""}> Alt som finnes</label>
<label class="opt"><input type="radio" name="mode" value="new"{" checked" if s.mode == "new" else ""}> Bare nye videoer fra nå</label>
<div class="fields">
<label class="field">Bare videoer fra og med<input type="date" name="cutoff" value="{s.cutoff.isoformat() if s.cutoff else ''}"></label>
<label class="field">Tittelen må inneholde (regex)<input type="text" name="title_regex" value="{_e(s.title_regex)}" placeholder="f.eks. Purified Radio"></label>
<label class="field">Minst (minutter)<input type="number" min="0" name="min_minutes" value="{mins(s.min_duration)}"></label>
<label class="field">Høyst (minutter)<input type="number" min="0" name="max_minutes" value="{mins(s.max_duration)}"></label></div>
<label class="opt"><input type="checkbox" name="shorts" value="1"{" checked" if s.shorts else ""}> Ta med Shorts</label>
<label class="opt"><input type="checkbox" name="livestreams" value="1"{" checked" if s.livestreams else ""}> Ta med opptak av direktesendinger</label>
</fieldset>
<fieldset><legend>Opprydding</legend><label class="opt">Behold de siste
<input type="number" name="keep_days" min="1" value="{s.keep_days or ''}" style="width:90px"> dager (tomt = behold alt)</label></fieldset>
<details><summary>Mer: SponsorBlock, undertekster, cookies</summary><div class="panel" style="margin-top:8px">
<div class="fields"><label class="field">SponsorBlock<select name="sponsorblock">{sb}</select></label>
<label class="field">Undertekster (språk, f.eks. nb,en)<input type="text" name="subtitles" value="{_e(s.subtitles)}"></label></div>
<div class="seasons">{sb_boxes}</div>
<label class="opt"><input type="checkbox" name="cookies" value="1"{" checked" if s.cookies else ""}> Bruk innloggings-cookies
<span class="muted">(bare for alders- eller medlemsbegrenset innhold)</span></label></div></details>"""

    def source_page(self, source_id: int) -> bytes | None:
        source = self.db.source(source_id)
        if not source:
            return None
        provider = providers.get(source.provider)
        rows = []
        for item in self.db.items(source.id):
            status = STATUS.get(item.status, item.status)
            if item.status == "failed" and item.attempts >= MAX_ATTEMPTS:
                status += " (gitt opp)"
            if item.status == "pending" and item.retry_after > time.time():
                status = "Prøver igjen " + datetime.fromtimestamp(item.retry_after).strftime("%d.%m %H:%M")
                if not item.error:
                    status = "Ikke lagt ut ennå"
            buttons = ""
            if item.status in ("failed", "expired", "geo", "removed", "skipped", "unavailable"):
                buttons += self._item_button("/prov-igjen", item, "Prøv igjen")
            if item.status in ("pending", "failed"):
                buttons += self._item_button("/hopp-over", item, "Hopp over")
            note = f'<div class="muted">{_e(item.error[-200:])}</div>' if item.error and item.status != "done" else ""
            link = f'<a href="{_e(provider.page_url(source, item))}">{_e(item.title)}</a>'
            rows.append(f"""<tr><td class="code">{_e(code(source, item))}</td><td>{link}{note}</td>
<td class="code">{_date(item.aired) if source.provider == "nrk" else _size(item.size)}</td>
<td class="code">{_date(item.expires)}</td><td>{_e(status)}</td><td style="white-space:nowrap">{buttons}</td></tr>""")
        table = (
            '<div class="tablewrap"><table><tr><th>Nr / dato</th><th>Tittel</th>'
            f'<th>{"Sendt" if source.provider == "nrk" else "Størrelse"}</th><th>Utløper</th><th>Status</th><th></th></tr>'
            + "".join(rows) + "</table></div>"
            if rows else '<div class="empty">Ingenting funnet ennå. Det dukker opp etter neste sjekk.</div>'
        )
        outlook = outlook_chip(source.series_type, source.last_aired, source.next_aired) if source.provider == "nrk" else ""
        feed = ""
        if source.layout == "audio":
            url = f"/feed/{source.feed_token}.xml"
            feed = f'<p>Podkast-feed: <a href="{url}">{url}</a> <span class="muted">(full adresse: denne sidens adresse + stien)</span></p>'
        body = f"""<div class="panel"><div class="hero">{picture(source.image or source.poster, source.folder_name)}
<div class="text"><h2 style="margin-top:0">{_e(source.folder_name)}</h2><div class="muted">{_e(source.plot[:600])}</div>
<p class="muted">{_e(kind_label(source))} · {_e(mode_text(source))}</p>{feed}
{f'<div class="chips">{outlook}</div>' if outlook else ""}
{"" if source.provider == "adhoc" or is_program(source) else f'<a class="btn" href="{self._edit_link(source)}">Endre</a>'}
{"" if source.provider == "adhoc" else f'<a class="btn" href="{_e(provider.page_url(source))}">Se hos {providers.NAMES[source.provider]}</a>'}
</div></div></div><h2>Innhold</h2>{table}"""
        return page(source.folder_name, body)

    def _item_button(self, action: str, item: Item, label: str) -> str:
        return f"""<form method="post" action="{action}" style="margin:0;display:inline">
<input type="hidden" name="provider" value="{item.provider}"><input type="hidden" name="item" value="{_e(item.item_id)}">
<input type="hidden" name="back" value="{item.source_id}"><button>{label}</button></form> """

    def log_page(self) -> bytes:
        rows = []
        for item, title in self.db.recent():
            when = datetime.fromtimestamp(item.updated).strftime("%d.%m %H:%M")
            note = f'<div class="muted">{_e(item.error[-200:])}</div>' if item.error and item.status != "done" else ""
            rows.append(f"""<tr><td class="code">{when}</td><td><span class="chip {item.provider}">{providers.NAMES[item.provider]}</span>
<a href="/kilde/{item.source_id}">{_e(title)}</a></td><td>{_e(item.title)}{note}</td><td>{_e(STATUS.get(item.status, item.status))}</td></tr>""")
        if not rows:
            return page("Logg", '<div class="empty">Ingenting har skjedd ennå.</div>')
        return page("Logg", '<h2>Siste hendelser</h2><div class="tablewrap"><table><tr><th>Når</th><th>Kilde</th>'
                    '<th>Hva</th><th>Status</th></tr>' + "".join(rows) + "</table></div>")

    # ---- actions ---------------------------------------------------------------

    def save_follow(self, form: dict[str, list[str]]) -> str:
        one = lambda key, default="": form.get(key, [default])[0].strip()
        provider, kind, ref = one("provider", "nrk"), one("kind"), one("ref")
        if provider not in providers.PROVIDERS or not ref:
            return "/"
        info = providers.get(provider).lookup(kind, ref)
        keep = one("keep_days")
        settings: dict = {"keep_days": int(keep) if keep.isdigit() and int(keep) > 0 else None}
        if provider == "nrk":
            mode = one("mode", "all")
            seasons = form.get("season", [])
            if mode == "seasons" and not seasons:
                mode = "all"
            settings.update(mode=mode if mode in MODES else "all", seasons=seasons,
                            layout="movie" if kind == "program" else "tv")
        else:
            layout = one("layout", "tv")
            minutes = lambda key: int(one(key)) * 60 if one(key).isdigit() and int(one(key)) > 0 else None
            regex = one("title_regex")
            try:
                re.compile(regex)
            except re.error:
                regex = re.escape(regex)
            quality = int(one("quality", "2160")) if one("quality", "2160").isdigit() else 2160
            settings.update(
                mode="new" if one("mode") == "new" else "all", layout=layout if layout in PRESETS else "tv",
                library=one("library"), folder=one("folder"), url=info.url,
                quality=0 if layout == "audio" else quality,
                cutoff=date.fromisoformat(one("cutoff")) if one("cutoff") else None, title_regex=regex,
                min_duration=minutes("min_minutes"), max_duration=minutes("max_minutes"),
                shorts=bool(one("shorts")), livestreams=bool(one("livestreams")),
                sponsorblock=one("sponsorblock") if one("sponsorblock") in ("", "mark", "remove") else "",
                sb_categories=",".join(c for c in form.get("sb_category", []) if c in SB_CATEGORIES) or "sponsor",
                subtitles=",".join(x.strip() for x in one("subtitles").split(",") if x.strip()),
                cookies=bool(one("cookies")),
            )
        existed = self.db.source_by_ref(provider, info.ref)
        source = self.db.save_source(
            provider, info.kind, info.ref, info.title, plot=info.plot, image=info.image, poster=info.poster,
            backdrop=info.backdrop, series_type=info.series_type, **settings,
        )
        if existed:
            self._forget_filtered(source)
        self.wake.set()
        return "/?ok=1"

    def _forget_filtered(self, source: Source) -> None:
        """New settings: what the old filters left out is looked at again on the next full listing."""
        marks = ",".join("?" * len(STICKY_SKIPS))
        with self.db.conn() as c:
            c.execute(
                f"DELETE FROM items WHERE source_id = ? AND status = 'skipped' AND COALESCE(error, '') NOT IN ({marks})",
                (source.id, *STICKY_SKIPS),
            )
            c.execute("UPDATE sources SET last_full_index = 0 WHERE id = ?", (source.id,))


def _base_url(handler: BaseHTTPRequestHandler) -> str:
    proto = handler.headers.get("X-Forwarded-Proto") or "http"
    return f"{proto}://{handler.headers.get('Host', 'localhost')}"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head: bool = False):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            one = lambda key, default="": q.get(key, [default])[0]
            try:
                if url.path.startswith("/feed/"):
                    return self._feed(url.path, head)
                if url.path == "/":
                    flash = "Lagret. Sjekker nå, nye ting lastes ned i bakgrunnen." if one("ok") else one("msg")
                    return self._send(200, app.index(flash, one("p") if one("p") in providers.PROVIDERS else ""))
                if url.path == "/health":
                    return self._send(200, json.dumps({"ok": True}).encode(), "application/json")
                if url.path == "/sok":
                    text = one("q")
                    if parsed := providers.parse_url(text):
                        provider, kind, ref = parsed
                        return self._redirect(f"/folg?provider={provider}&kind={kind}&ref={urllib.parse.quote(ref)}")
                    return self._send(200, app.search(text, youtube=bool(one("yt"))))
                if url.path == "/folg":
                    return self._send(200, app.follow_form(one("provider", "nrk"), one("kind", "series"), one("ref")))
                if url.path.startswith("/kilde/") and url.path[7:].isdigit():
                    if body := app.source_page(int(url.path[7:])):
                        return self._send(200, body)
                if url.path.startswith("/serie/") and url.path[7:].isdigit():  # links from before the rename
                    return self._redirect(f"/kilde/{url.path[7:]}")
                if url.path == "/logg":
                    return self._send(200, app.log_page())
                self._send(404, page("Ikke funnet", '<div class="empty">Siden finnes ikke.</div>'))
            except Exception as e:
                log.exception("web: GET %s failed", self.path)
                self._send(500, page("Feil", f'<div class="empty">Noe gikk galt: {_e(e)}</div>'))

        def _feed(self, path: str, head: bool) -> None:
            m = re.fullmatch(r"/feed/([\w-]+)(?:\.xml|/([\w-]+)\.\w+)", path)
            source = app.db.source_by_feed(m[1]) if m else None
            if not source or source.layout != "audio":
                return self._send(404, b"Not found", "text/plain")
            if not m[2]:
                return self._send(200, feeds.render(app.db, source, _base_url(self)), "application/rss+xml; charset=utf-8")
            item = app.db.item(source.provider, m[2])
            file = feeds.media_file(item) if item and item.source_id == source.id else None
            if not file:
                return self._send(404, b"Not found", "text/plain")
            self._serve_file(file, head)

        def _serve_file(self, file, head: bool) -> None:
            """With byte ranges: podcast apps seek and resume."""
            size = file.stat().st_size
            start, end = 0, size - 1
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if rng and (rng[1] or rng[2]):
                if rng[1]:
                    start, end = int(rng[1]), int(rng[2]) if rng[2] else size - 1
                else:
                    start = max(0, size - int(rng[2]))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", feeds.TYPES.get(file.suffix.lower(), "application/octet-stream"))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if head:
                return
            with open(file, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := f.read(min(1 << 20, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _api_fetch(self, url: str, kind: str) -> None:
            """For the iOS shortcut (and anything else): {"url": ..., "type": "video" | "audio"}."""
            try:
                msg = app.fetch(url, audio=kind.lower() in ("audio", "lyd", "bare lyd"))
                status, body = 200, {"ok": True, "message": msg}
            except Exception as e:
                status, body = 400, {"ok": False, "message": f"Kunne ikke hente: {e}"}
            self._send(status, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            if "json" in (self.headers.get("Content-Type") or ""):
                try:
                    form = {k: [str(v)] for k, v in json.loads(raw or "{}").items()}
                except ValueError:
                    form = {}
            else:
                form = urllib.parse.parse_qs(raw)
            one = lambda key: form.get(key, [""])[0]
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/api/hent":
                    return self._api_fetch(one("url"), one("type"))
                if path == "/hent":
                    try:
                        msg = app.fetch(one("url"), audio=one("type") == "audio")
                    except Exception as e:
                        msg = f"Kunne ikke hente: {e}"
                    return self._redirect("/?msg=" + urllib.parse.quote(msg))
                if path == "/folg":
                    return self._redirect(app.save_follow(form))
                if path == "/kjor":
                    app.wake.set()
                    return self._redirect("/")
                if path in ("/pause", "/slett") and one("id").isdigit():
                    source = app.db.source(int(one("id")))
                    if source and path == "/pause":
                        app.db.set_enabled(source.id, not source.enabled)
                    elif source:
                        app.db.delete_source(source.id)
                    return self._redirect("/")
                if path in ("/prov-igjen", "/hopp-over") and one("item"):
                    if path == "/prov-igjen":
                        app.db.retry(one("provider"), one("item"))
                        app.wake.set()
                    else:
                        app.db.skip(one("provider"), one("item"))
                    back = one("back")
                    return self._redirect(f"/kilde/{back}" if back.isdigit() else "/")
                self._send(404, page("Ikke funnet", '<div class="empty">Siden finnes ikke.</div>'))
            except Exception as e:
                log.exception("web: POST %s failed", self.path)
                self._send(500, page("Feil", f'<div class="empty">Noe gikk galt: {_e(e)}</div>'))

    return Handler


def serve(app: App, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log.info("Web page on port %d", port)
    return server
