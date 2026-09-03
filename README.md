# Tehran Life Heatmap

A personal familiarity map: click places you've lived, frequented, traveled to, visited, or briefly stopped by, and get back a heatmap where each spot glows with a radius and intensity scaled to how well you actually know it.

![Demo heatmap](docs/demo_heatmap.png)

*(Demo image generated from the synthetic `data/locations.example.csv` — no real personal data is included in this repo.)*

## How it works

Instead of a generic "distance decay" heatmap, each point's glow radius and color intensity come from a small model of how spatial familiarity actually builds up: fast at first, then leveling off with more exposure (a saturating exponential — the shape spatial-cognition research uses for how people build a mental map of a place), with different ceilings depending on *how* you were exposed to it.

| Category | Radius range | Saturates around | What it's for |
|---|---|---|---|
| **Lived** | 250m → 1200m | ~1 year | You resided there — familiarity grows to a walkable "pedshed" around home. |
| **Frequented** | 150m → 700m | ~10 months | A routine anchor point over an extended period without living there — school, a job, a language institute. |
| **Traveled** | 150m → 500m | ~2 days | A short trip outside your routine (days to weeks) — you cover real ground sightseeing, but it's temporary. |
| **Visited** | 80m → 350m | ~6 months | Repeat point-to-point trips to one destination — a friend's place, a regular café. |
| **Guest** | 40m → 120m | ~6 hours | A one-off visit of a few hours — familiarity never leaves the venue. |

These ranges are an informed heuristic (grounded in pedestrian-catchment/"pedshed" urban planning standards and spatial-knowledge-acquisition research), not a measured constant — tune them freely in `heatmap.py`'s `CATEGORY_PARAMS`.

## Getting started

This section assumes no prior setup — if you already have Python and Git, skip to [Install and run](#install-and-run).

### What you'll need

This is a program you run on your own computer (it's not a website) — you'll need two things installed first: **Python** (the language it's written in) and, ideally, **Git** (a tool for downloading code from GitHub — though you can skip it and just download a ZIP instead, see below). Both are free. Everything below is done in your computer's **terminal** — a text-based window for typing commands instead of clicking. That's normal for a project like this one; you don't need to know anything about it beyond copy-pasting the commands shown.

#### 1. Install Python (if you don't have it)

Check first — open a terminal (see "Opening a terminal" below) and type `python3 --version` (Windows: `python --version`). If it prints `Python 3.10` or higher, you're set and can skip ahead.

- **Windows**: download the installer from [python.org/downloads](https://www.python.org/downloads/). While installing, make sure to tick **"Add python.exe to PATH"** on the first screen — easy to miss, and without it the commands below won't work.
- **macOS**: download the installer from [python.org/downloads](https://www.python.org/downloads/). (macOS ships with an old Python 2 as `python` — this project needs 3.10+, hence `python3` in the commands below.)
- **Linux**: almost always already installed. If not, install it with your distro's package manager, e.g. `sudo apt install python3 python3-venv` on Ubuntu/Debian.

#### 2. Opening a terminal

- **Windows**: press the Start key, type `PowerShell`, press Enter.
- **macOS**: press `Cmd+Space`, type `Terminal`, press Enter.
- **Linux**: usually `Ctrl+Alt+T`, or search "Terminal" in your applications menu.

#### 3. Get the code

**With Git** (if you have it, or want it — installers at [git-scm.com/downloads](https://git-scm.com/downloads)):

```bash
git clone <this-repo-url>
cd life-heatmap
```

**Without Git** — on this repository's GitHub page, click the green **Code** button → **Download ZIP**, then unzip it anywhere. In the terminal, `cd` into the folder you unzipped (drag the folder into the terminal window after typing `cd ` — most terminals will fill in the path for you), e.g.:

```bash
cd Downloads/life-heatmap-main
```

### Install and run

Pick your OS:

**Linux / macOS**

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py
```

**Windows (PowerShell or cmd)**

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

(If `py` isn't found, use `python` instead — depends on how Python was installed.)

The middle line downloads this project's dependencies into a private folder (`.venv`) inside the project — it doesn't touch anything else on your system, and re-running it is safe. It only needs to be run once; after that, you can just re-run the last line (`app.py`) each time you want to use the app.

Once it's running, open **http://127.0.0.1:5000** in your browser, click anywhere on the map, pick a category and how long you were there, and save. A live dashed circle previews the exact radius before you commit. Hit **Generate Heatmap** any time to render the current glow over the map, or open the full standalone version it links to. To stop the app, go back to the terminal and press `Ctrl+C`.

The basemap (streets, place labels) is served live by Esri's free "Light Gray Canvas" tiles — no API key or account needed, and none of the steps above touch a third-party service beyond that.

## Command-line usage (no interactive collector)

```bash
./.venv/bin/python heatmap.py --input data/locations.csv --output output/tehran_life_heatmap.html
```

On Windows, use `.venv\Scripts\python` instead of `./.venv/bin/python`.

Add `--skip-geocode` if every row already has `lat`/`lon` filled in (otherwise addresses are resolved via OpenStreetMap Nominatim and the coordinates are cached back into the CSV).

## Sharing your heatmap

On the full standalone map (open it from the collector page, or run the CLI above), a **"Download shareable image"** button in the bottom-right renders a single high-resolution PNG — the map plus a title/description card — sized for posting on social media. It needs the app running (`python app.py`) since rendering happens server-side; opened as a bare file it'll tell you so instead of failing silently. The first render for a given map extent fetches basemap tiles fresh (a few seconds); after that they're cached locally (`.tile_cache/`, git-ignored) and it's near-instant.

## Your data stays yours

`data/locations.csv` (your real places) and `output/` (the maps generated from them) are both **git-ignored** — see `.gitignore`. The only location data tracked in this repo is `data/locations.example.csv`, a small synthetic dataset used to generate the demo image above. If you fork or clone this project, your own places never get committed unless you deliberately remove them from `.gitignore`.

## Project structure

```
heatmap.py                  Core model: familiarity math, density grid, PNG/HTML/share-image rendering, CLI
app.py                      Flask backend for the interactive collector
templates/index.html        Click-to-add map UI (Leaflet)
data/locations.example.csv  Synthetic demo dataset (tracked)
data/locations.csv          Your real places (git-ignored, created on first save)
output/                     Generated heatmaps (git-ignored)
.tile_cache/                Cached basemap tiles for the share-image export (git-ignored)
docs/demo_heatmap.png       README screenshot, built from the example dataset
```

## Using this for another city

Everything here is Tehran-flavored by default (`TEHRAN_CENTER` in `heatmap.py`), but nothing in the model is Tehran-specific. Change that one constant to re-center the map anywhere else.

## Requirements

Python 3.10+, and the packages in `requirements.txt` (Flask, pandas, numpy, folium, matplotlib, geopy, requests, Pillow).

## License

MIT — see [LICENSE](LICENSE).
