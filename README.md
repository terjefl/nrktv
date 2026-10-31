# mediafang

Follow series and films on NRK TV and channels and playlists on YouTube, and keep them in their
own Plex and Jellyfin libraries. One page (media.flagan.net) to find things and choose how to
follow them; the container checks every hour, downloads with yt-dlp and files everything with
Kodi-style NFO files, artwork and subtitles, so both servers show the right show, season,
episode, title, date and description without matching against TVDB/TMDB.

It replaces Pinchflat for YouTube (and took over its library), and was nrktv before that.

## NRK TV

- **Metadata** from NRK's catalogue API (psapi.nrk.no); **video and subtitles** from yt-dlp's
  NRK extractor.
- Per series: *Alt som finnes*, *Valgte sesonger* (for date series: years) or *Bare nye*.
- **Date series** (Lindmo, Dagsrevyen) have no episode numbers at NRK: the season is the year and
  the episode number is made from the date, 22 October = `S2026E102201`. They can keep only the
  last N days.
- **Films and single programs** go to a separate movie library. They are downloaded once, not
  followed: *Last ned* instead of *Følg*, and a list of their own under the series on the page.
- **Is there more coming?** NRK has no flag for a series that has ended, but it lists the next
  episode ahead of time. Each series shows *ny episode* with its date, the date of the latest one,
  or *trolig avsluttet* when nothing is listed and the latest is over half a year old (talk shows
  and news) or two years old (drama, which can go a year between seasons).
- Episodes whose rights end soonest are downloaded first. Broadcasts NRK lists while they are
  still on air wait until the recording is out; ended rights and Norway-only content are marked.

## YouTube

- **Listing is cheap**: a flat listing of the channel's tabs (one paged request per tab, not one
  per video) at most once a day, stopping at known videos or before the oldest date wanted, and
  the channel's RSS feed in between. Exact date, duration, shorts and livestreams are checked on
  the video itself right before downloading.
- **Three ways to file a source**, each its own library folder under `youtube_dir`:
  - *Serie (TV)*: `<library>/<channel>/Season 2026/s2026e102299 - <title> [<id>].mp4` with NFO,
    poster (channel avatar) and background (banner). Numbering as Pinchflat did.
  - *Videoer*: `<library>/<channel>/<YYYY-MM-DD> <title>/<title> [<id>].mp4`, for DJ sets and the like.
  - *Bare lyd*: `<library>/<channel>/<YYYY-MM-DD> <title> [<id>].m4a`, plus a podcast feed at
    `/feed/<token>.xml` (the token is random; this path is the only one open without Cloudflare Access).
- **Filters**: start date, title regex, min/max duration, shorts, livestreams, *Bare nye*, keep N days.
- **SponsorBlock**: mark segments as chapters or cut them out.
- Private, deleted and members-only videos are marked and never retried; when YouTube throttles,
  YouTube downloads stop for that run and the video waits (30 min, doubling up to a day).
- Cookies (`cookies_file`) only for sources that ask for them; an optional bgutil PO-token
  provider via `pot_provider_url`. Deno is in the image for YouTube's JavaScript challenges.

## One-off downloads (what MeTube did)

Paste a link to a single video (YouTube or anything yt-dlp knows) in «Hent én video» on the
page, or in the search field, and choose **Video** or **Bare lyd**. Videos go to
`<youtube_dir>/Enkeltvideoer/<channel>/<YYYY-MM-DD> <title>/`, audio to `<youtube_dir>/Lyd/Enkelt/`
(with its own podcast feed). A playlist link adds its videos (at most 100). One-off downloads go
first in the queue.

### API and the iOS shortcut

`POST /api/hent` with JSON `{"url": "...", "type": "video" | "audio"}` queues a link and answers
`{"ok": true, "message": "Lastes ned: <title>"}`. On media.flagan.net, `/api/*` is its own
Cloudflare Access app that only lets through the service token «mediafang iOS-snarvei», so the
call needs the headers `CF-Access-Client-Id` and `CF-Access-Client-Secret`.

`tools/make_shortcut.py` builds the shortcut «Send til Mediafang» with that token: in the share
sheet it asks Video or Bare lyd, sends the link and shows the answer as a notification (run on
its own, it asks for a link). Sign it with `shortcuts sign --mode anyone` and AirDrop it to the
phone. The file holds the token, so it is kept out of git (`ios/`, `*.shortcut`); to revoke it,
delete or rotate the service token in Cloudflare Zero Trust and build a new shortcut.

## Plex and Jellyfin

Libraries are found by their folders: after a download the changed folder is scanned in whichever
Plex library holds it, and reported to Jellyfin. TV-style folders want the *Plex NFO Series*
agent (Jellyfin: Shows with the NFO reader on and online downloaders off); *Videoer* folders a
*Plex Video Files* / *Home videos* library.

## Taking over Pinchflat

`mediafang migrate-pinchflat` shows what it would do: every Pinchflat source with its settings,
and where its folder moves (`<media_root>/Pinchflat/...` → `<youtube_dir>/<library>/<channel>`).
With `--apply` (Pinchflat stopped) it imports the sources and videos (nothing is downloaded again),
adds the new folders to the Plex/Jellyfin libraries that hold the old ones, and moves each folder
with a single rename on the media share. After the libraries have scanned, `--finish` removes the
old folders from them. See `pinchflat_libraries` in the config for library folder names.

## Running

```
cp config.template.yaml config.yaml    # paths, tokens
uv run mediafang --config config.yaml add https://tv.nrk.no/serie/fleksnes --mode seasons --seasons 1
uv run mediafang --config config.yaml add https://www.youtube.com/@NoraEnPure --preset videos
uv run mediafang --config config.yaml run
uv run mediafang --config config.yaml loop  # web page on web_port, plus a run every interval_minutes
```

Other commands: `search <text> [--youtube]`, `list`, `scan`, `refresh-art`, `web`, `migrate-pinchflat`.

In Docker: see `compose.example.yml`. The image is built by GitHub Actions to
`ghcr.io/terjefl/mediafang` on every push to `main`, and weekly for a fresh yt-dlp. The page has
no login of its own: it belongs behind Cloudflare Access.
