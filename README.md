# Spelunky PB Tracker

A PB/WR tracker and OBS overlay for Spelunky HD speedrunning. It watches
[HD Toolbox](https://github.com/derrp-tf/hd-toolbox) live while you play,
shows your current category/PB/rank, and can overlay your splits (real
measured splits for the run in progress, or the world record's estimated
splits) right on stream.

## Features

- **Live overlay** - category, PB time, rank, and rating, driven by HD
  Toolbox. Drop it into OBS as a transparent browser source.
- **Live splits** - while HD Toolbox is tracking a run, the splits column
  shows your real measured per-area time as each one completes, instead of
  just your stored PB - falls back to the PB/estimate for anything the
  run hasn't reached yet.
- **MossRanking sync** - pulls your PBs/ranks/ratings straight from your
  [mossranking.com](https://mossranking.com) profile.
- **Split estimates** - no stopwatch-tracked splits? "Estimate PB Splits"
  scales a known real run's splits to your own PB time (with a
  configurable Olmec-fight assumption) to get a reasonable guess.
- **World Records** - every category's current WR (holder + time), scraped
  from MossRanking, with its own estimated split breakdown. Editable by
  hand if you know a WR's real splits more precisely - correcting one
  split rescales the others around it instead of just overriding the one
  value, and the correction is reused for every future estimate (both the
  WR estimate and a regular player's own PB estimate) for that category.
  A refresh button re-scrapes MossRanking for anything that's changed.
- **OBS-ready links** - generates a browser source URL (full size, with
  toggleable PB/WR/category/rank/rating columns) and a compact "overlay
  only" link for personal use.

## Quick start (no Python required)

1. Download the latest `PBTracker.exe` from this repo's
   [Releases](../../releases) page.
2. Run it - it opens the dashboard in your browser and sits in your system
   tray while it runs. Keep it running while you play.
3. Sync your MossRanking profile (top of the dashboard).
4. Copy the Browser Source link into OBS as a Browser Source
   (1920x1080), or use the Personal Use Only link if you just want the
   overlay for yourself without OBS.
5. Have [HD Toolbox](https://github.com/derrp-tf/hd-toolbox) running too,
   for live category detection and live splits - the overlay still works
   without it, just without knowing which category you're playing or
   showing real-time split progress.

## Running from source

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python server.py
```

or, on Windows, just double-click `run.bat`.

Then open `http://localhost:4226`.

## Building the exe yourself

```bash
pip install pyinstaller
pyinstaller PBTracker.spec
```

The built `PBTracker.exe` lands in `dist/`.

## The split-estimate data

`reference_splits.json` is one real runner's own actual splits per
category (used as the baseline every estimate scales from), and
`world_records.json` is the current WR holder + time for every category
(scraped from MossRanking). Both ship with this repo so estimates work
out of the box - no setup needed.

## License

[MIT](LICENSE)
