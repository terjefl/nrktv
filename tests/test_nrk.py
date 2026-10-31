import json
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from mediafang.config import Config
from mediafang.db import DB, Item
from mediafang.harvest import Harvester, _failure_status
from mediafang.library import subtitle_name, target, write_episode_nfo, write_movie_nfo
from mediafang.providers import nrk as planner
from mediafang.providers import nrk_api as psapi
from mediafang.titles import date_title_suffix, strip_number

FIXTURES = Path(__file__).parent / "fixtures"
NRK_ITEM = Item(provider="nrk", item_id="X", source_id=0, season=0, episode=0, title="")
PATHS = {
    "/tv/catalog/series/fleksnes": "series-fleksnes.json",
    "/tv/catalog/series/lindmo": "series-lindmo.json",
    "/tv/catalog/series/dagsrevyen": "series-dagsrevyen.json",
    "/tv/catalog/series/fleksnes/seasons/1": "season-fleksnes-1.json",
    "/tv/catalog/series/lindmo/seasons/2024": "season-lindmo-2024.json",
    "/tv/catalog/series/dagsrevyen/seasons/202610": "season-dagsrevyen-202610.json",
    "/tv/catalog/programs/KOID22008424": "program-KOID22008424.json",
}


@pytest.fixture
def fake_api(monkeypatch):
    def get(path):
        if path.startswith("/search"):
            return json.loads((FIXTURES / "search.json").read_text())
        if path in PATHS:
            return json.loads((FIXTURES / PATHS[path]).read_text())
        # seasons we have no recording of: empty
        return {"_embedded": {"episodes": []}, "_links": {}}

    monkeypatch.setattr(psapi, "get", get)


@pytest.fixture
def cfg(tmp_path):
    return Config(library_dir=tmp_path / "lib", movies_dir=tmp_path / "movies", data_dir=tmp_path / "data")


@pytest.fixture
def db(cfg):
    return DB(cfg.db_path)


def test_titles():
    assert strip_number("1. Fysiske fordeler") == "Fysiske fordeler"
    assert strip_number("4. episode") == "4. episode"
    assert strip_number("Uten nummer") == "Uten nummer"
    assert date_title_suffix("31. august 2001 · Fleksnes") == "Fleksnes"
    assert date_title_suffix("22. oktober") == ""


def test_parse_url():
    assert psapi.parse_url("https://tv.nrk.no/serie/fleksnes/sesong/1/episode/2") == ("series", "fleksnes")
    assert psapi.parse_url("https://tv.nrk.no/serie/side-om-side/MUHH50000213") == ("series", "side-om-side")
    assert psapi.parse_url("https://tv.nrk.no/program/KOID22008424") == ("program", "KOID22008424")
    assert psapi.parse_url("koid22008424") == ("program", "KOID22008424")
    assert psapi.parse_url("fleksnes") is None


def test_series_and_seasons(fake_api):
    s = psapi.series("fleksnes")
    assert (s.title, s.series_type, s.dated) == ("Fleksnes", "sequential", False)
    assert [x.name for x in s.seasons][:2] == ["1", "2"]
    eps = psapi.season("fleksnes", "1")
    assert eps[0].prf_id == "FUHA01005272" and eps[0].sequence == 1
    assert eps[0].aired == date(1972, 11, 22)
    assert eps[0].expires is None  # "Alltid tilgjengelig"
    news = psapi.season("dagsrevyen", "202610")
    assert news[0].aired == date(2026, 10, 22) and not news[0].available  # "Kommer i morgen"


def test_program(fake_api):
    p = psapi.program("KOID22008424")
    assert p.title == "Kongehusets hemmelige millioner"
    assert p.year == 2024 and p.geo_blocked and p.expires == date(2027, 11, 9)


def test_search(fake_api):
    hits = psapi.search("fleksnes")
    assert hits[0].kind == "series" and hits[0].ref == "fleksnes"
    assert any(h.kind == "program" for h in hits)
    assert all(h.kind in ("series", "program") for h in hits)  # episodes are left out


def test_plan_sequential(fake_api, cfg, db):
    s = psapi.series("fleksnes")
    follow = db.save_source("nrk", "series", "fleksnes", s.title, series_type=s.series_type)
    inst = psapi.season("fleksnes", "1")[0]
    ep = planner.plan(follow, s, s.seasons[0], 0, inst)
    assert (ep.season, ep.episode, ep.title) == (1, 1, "Fysiske fordeler")
    t = target(cfg, follow, ep)
    assert t.folder == cfg.library_dir / "Fleksnes" / "Season 01"
    assert t.basename == "Fleksnes - S01E01 - Fysiske fordeler"


def test_plan_dated_numbers_from_date(fake_api, cfg, db):
    s = psapi.series("lindmo")
    follow = db.save_source("nrk", "series", "lindmo", s.title, series_type=s.series_type)
    ref = next(r for r in s.seasons if r.name == "2024")
    inst = psapi.season("lindmo", "2024")[0]
    ep = planner.plan(follow, s, ref, 0, inst)
    assert (ep.season, ep.episode, ep.aired) == (2024, 40501, date(2024, 4, 5))
    t = target(cfg, follow, ep)
    assert t.folder.name == "Season 2024"
    assert t.basename == "Lindmo - S2024E040501 - 2024-04-05"
    # a second broadcast the same day gets the next number
    assert db.add_item(ep, unique_number=True)
    other = planner.plan(follow, s, ref, 1, inst)
    other.item_id = "OTHER0000001"
    assert db.add_item(other, unique_number=True)
    assert sorted(e.episode for e in db.items(follow.id)) == [40501, 40502]


def test_known_episode_keeps_its_number(fake_api, db):
    s = psapi.series("fleksnes")
    follow = db.save_source("nrk", "series", "fleksnes", s.title, series_type=s.series_type)
    ep = planner.plan(follow, s, s.seasons[0], 0, psapi.season("fleksnes", "1")[0])
    assert db.add_item(ep)
    ep.episode = 9
    assert not db.add_item(ep)
    assert db.item("nrk", ep.item_id).episode == 1


def test_seasons_to_scan(fake_api, db):
    s = psapi.series("fleksnes")
    follow = db.save_source("nrk", "series", "fleksnes", s.title, mode="seasons", seasons=["1", "3"], series_type=s.series_type)
    names = [r.name for r in planner.seasons_to_scan(follow, s, {})]
    assert names == ["1", "3"]
    # scanned seasons wait a week, except the newest
    now = 1_000_000.0
    names = [r.name for r in planner.seasons_to_scan(follow, s, {"1": now, "3": now}, now=now)]
    assert names == []
    follow = db.save_source("nrk", "series", "fleksnes", s.title, mode="new")
    assert [r.name for r in planner.seasons_to_scan(follow, s, {})] == ["7"]


def test_seasons_to_scan_dated_keep_days(fake_api, db):
    s = psapi.series("dagsrevyen")
    follow = db.save_source("nrk", "series", "dagsrevyen", s.title, keep_days=40, series_type=s.series_type)
    names = [r.name for r in planner.seasons_to_scan(follow, s, {})]
    # 602 months of Dagsrevyen, but only those that reach into the last 40 days
    assert 1 <= len(names) <= 3 and all(n >= "2026" for n in names)
    follow = db.save_source("nrk", "series", "dagsrevyen", s.title, mode="seasons", seasons=["2025"], keep_days=None)
    assert {r.name[:4] for r in planner.seasons_to_scan(follow, s, {})} >= {"2025"}


def test_wanted(fake_api, db):
    s = psapi.series("lindmo")
    inst = psapi.season("lindmo", "2024")[0]
    follow = db.save_source("nrk", "series", "lindmo", s.title, series_type=s.series_type)
    assert planner.wanted(follow, inst)
    follow = db.save_source("nrk", "series", "lindmo", s.title, mode="new", series_type=s.series_type)
    assert not planner.wanted(follow, inst)  # published before we started following
    follow = db.save_source("nrk", "series", "lindmo", s.title, keep_days=30, series_type=s.series_type)
    assert not planner.wanted(follow, inst)  # 2024 is too old


def test_scan_records_episodes(fake_api, cfg, db):
    follow = db.save_source("nrk", "series", "fleksnes", "Fleksnes")
    h = Harvester(cfg, db)
    assert h.scan(follow) == 5
    assert h.scan(db.source(follow.id)) == 0
    assert db.source(follow.id).series_type == "sequential"
    due = db.due()
    assert [e.episode for e in due] == [1, 2, 3, 4, 5]


def test_scan_program(fake_api, cfg, db):
    follow = db.save_source("nrk", "program", "KOID22008424", "Kongehusets hemmelige millioner")
    assert Harvester(cfg, db).scan(follow) == 1
    ep = db.item("nrk", "KOID22008424")
    t = target(cfg, db.source(follow.id), ep)
    assert t.folder == cfg.movies_dir / "Kongehusets hemmelige millioner (2024)"
    write_movie_nfo(t.nfo, ep)
    root = ET.parse(t.nfo).getroot()
    assert root.findtext("title") == "Kongehusets hemmelige millioner"
    assert root.findtext("originaltitle") == "The King, The Prince and their Secret Millions"
    assert root.find("uniqueid").text == "KOID22008424"


def test_episode_nfo(fake_api, cfg, db, tmp_path):
    s = psapi.series("fleksnes")
    follow = db.save_source("nrk", "series", "fleksnes", s.title, plot=s.plot, series_type=s.series_type)
    ep = planner.plan(follow, s, s.seasons[0], 0, psapi.season("fleksnes", "1")[0])
    write_episode_nfo(tmp_path / "e.nfo", follow, ep)
    root = ET.parse(tmp_path / "e.nfo").getroot()
    assert root.findtext("showtitle") == "Fleksnes"
    assert root.findtext("aired") == "1972-11-22"
    assert (root.findtext("season"), root.findtext("episode")) == ("1", "1")
    assert root.find("uniqueid").get("type") == "nrk"


def test_subtitle_names():
    assert subtitle_name("X - S01E01", "nb-ttv") == "X - S01E01.nb.srt"
    assert subtitle_name("X - S01E01", "nb-nor") == "X - S01E01.nb.forced.srt"
    assert subtitle_name("X - S01E01", "en") == "X - S01E01.en.srt"


def test_failure_status():
    from yt_dlp.utils import DownloadError

    assert _failure_status(NRK_ITEM, DownloadError("ERROR: [NRK] X: NRK said: Programmet har gått ut")) == "expired"
    assert _failure_status(NRK_ITEM, DownloadError("NRK har ikke rettigheter til å vise dette programmet utenfor Norge")) == "geo"
    assert _failure_status(NRK_ITEM, DownloadError("HTTP Error 500")) == "failed"


def test_failed_waits_and_gives_up(fake_api, cfg, db):
    follow = db.save_source("nrk", "program", "KOID22008424", "K")
    Harvester(cfg, db).scan(follow)
    db.set_status(db.item("nrk", "KOID22008424"), "failed", error="boom")
    assert db.due() == []  # waits an hour first
    db.retry("nrk", "KOID22008424")
    assert [e.item_id for e in db.due()] == ["KOID22008424"]


def test_retention(fake_api, cfg, db):
    follow = db.save_source("nrk", "series", "lindmo", "Lindmo", keep_days=30, series_type="standard")
    s = psapi.series("lindmo")
    ref = next(r for r in s.seasons if r.name == "2024")
    ep = planner.plan(follow, s, ref, 0, psapi.season("lindmo", "2024")[0])
    db.add_item(ep, unique_number=True)
    t = target(cfg, follow, ep)
    t.folder.mkdir(parents=True)
    files = [t.folder / f"{t.basename}.mp4", t.nfo, t.folder / f"{t.basename}.nb.srt", t.folder / f"{t.basename}-thumb.jpg"]
    for f in files:
        f.write_text("x")
    db.set_status(ep, "done", files=[str(files[0])])
    assert Harvester(cfg, db).apply_retention()[0] == 1
    assert not any(f.exists() for f in files)
    assert not t.folder.exists()
    assert db.item("nrk", ep.item_id).status == "removed"


def test_follow_mode_new_since(fake_api, db):
    follow = db.save_source("nrk", "series", "dagsrevyen", "Dagsrevyen", mode="new")
    assert abs(follow.since.timestamp() - datetime.now().timestamp()) < 60
    # changing the mode keeps the original start
    again = db.save_source("nrk", "series", "dagsrevyen", "Dagsrevyen", mode="all")
    assert again.since == follow.since and again.mode == "all"


def test_not_out_yet_waits_without_error(fake_api, cfg, db):
    from yt_dlp.utils import DownloadError

    assert _failure_status(NRK_ITEM, DownloadError("ERROR: [NRK] NNFA51102226: NRK said: Kommer i dag kl. 21:11")) == "waiting"
    follow = db.save_source("nrk", "program", "KOID22008424", "K")
    Harvester(cfg, db).scan(follow)
    db.postpone(db.item("nrk", "KOID22008424"), time.time() + 1800)
    ep = db.item("nrk", "KOID22008424")
    assert (ep.status, ep.error, ep.attempts) == ("pending", None, 0)
    assert db.due() == []  # not before the wait is over


def test_broadcast_before_rights_start_is_skipped(fake_api, db):
    s = psapi.series("lindmo")
    follow = db.save_source("nrk", "series", "lindmo", s.title, series_type=s.series_type)
    inst = psapi.season("lindmo", "2024")[0]
    inst.rights_from = datetime.now().astimezone() + timedelta(hours=2)
    assert not planner.wanted(follow, inst)


def test_ready_at(monkeypatch):
    soon = (datetime.now().astimezone() + timedelta(minutes=20)).isoformat()
    manifests = {
        "LIVE": {"availability": {"onDemand": None, "live": {"isOngoing": True, "transmissionInterval": {"to": soon}}}},
        "READY": {"availability": {"onDemand": {"hasRightsNow": True}, "live": None}},
        "LATER": {"availability": {"onDemand": {"from": soon, "hasRightsNow": False}, "live": None}},
    }
    monkeypatch.setattr(psapi, "get", lambda path: manifests[path.rsplit("/", 1)[1]])
    assert psapi.ready_at("READY") is None
    assert psapi.ready_at("LATER").isoformat() == soon
    assert psapi.ready_at("LIVE") > datetime.fromisoformat(soon)  # after the broadcast ends


def test_ready_at_waits_for_the_recording(monkeypatch):
    live = "https://nrk-live-world.akamaized.net/nrk1-x/muxed.m3u8"
    edge = "https://cdn0-47115-liveedge1.dna.contentdelivery.net/47115-liveedge1/nrk1-x"
    od = "https://nrkod26-cdn0-47115-odedge1.dna.contentdelivery.net/x/master.m3u8"
    manifests = {
        "LIVECOPY": {"availability": {"onDemand": {"hasRightsNow": True}}, "playable": {"assets": [{"url": live}]}},
        "EDGECOPY": {"availability": {"onDemand": {"hasRightsNow": True}}, "playable": {"assets": [{"url": edge}]}},
        "RECORDING": {"availability": {"onDemand": {"hasRightsNow": True}}, "playable": {"assets": [{"url": od}]}},
    }
    monkeypatch.setattr(psapi, "get", lambda path: manifests[path.rsplit("/", 1)[1]])
    assert psapi.ready_at("LIVECOPY") > datetime.now().astimezone()
    assert psapi.ready_at("EDGECOPY") > datetime.now().astimezone()
    assert psapi.ready_at("RECORDING") is None


def test_outlook(fake_api, monkeypatch):
    lindmo = psapi.series("lindmo")
    last, upcoming = planner.outlook(lindmo)
    assert upcoming is None
    assert last == max(i.aired for i in psapi.season("lindmo", "2024"))

    soon = datetime.now().astimezone() + timedelta(days=6)
    coming = psapi.Instalment("X", "", "", None, None, soon, None, False, False, None, rights_from=soon, coming=True)
    assert planner.outlook(lindmo, {"2024": [coming], "2023": []}) == (None, soon.date())


def test_scan_stores_outlook(fake_api, cfg, db):
    follow = db.save_source("nrk", "series", "lindmo", "Lindmo")
    Harvester(cfg, db).scan(follow)
    assert db.source(follow.id).last_aired == planner.outlook(psapi.series("lindmo"))[0]


def test_done_program_is_not_looked_up_again(fake_api, cfg, db, monkeypatch):
    follow = db.save_source("nrk", "program", "KOID22008424", "Kongehusets hemmelige millioner")
    h = Harvester(cfg, db)
    h.scan(follow)
    db.set_status(db.item("nrk", "KOID22008424"), "done")
    monkeypatch.setattr(psapi, "get", lambda path: pytest.fail(f"looked up {path}"))
    assert h.scan(follow) == 0


def test_outlook_chip():
    from mediafang.web import outlook_chip

    today = date.today()
    assert "ny episode" in outlook_chip("sequential", today, today + timedelta(days=3))
    assert "trolig avsluttet" in outlook_chip("sequential", today - timedelta(days=800), None)
    assert "trolig avsluttet" not in outlook_chip("sequential", today - timedelta(days=400), None)
    assert "trolig avsluttet" in outlook_chip("standard", today - timedelta(days=200), None)
    assert outlook_chip("sequential", None, None) == ""
