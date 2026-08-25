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

```bash
git clone <this-repo-url>
cd life-heatmap
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py
```

Open **http://127.0.0.1:5000**, click anywhere on the map, pick a category and how long you were there, and save. A live dashed circle previews the exact radius before you commit. Hit **Generate Heatmap** any time to render the current glow over the map, or open the full standalone version it links to.

## Command-line usage (no interactive collector)

```bash
./.venv/bin/python heatmap.py --input data/locations.csv --output output/tehran_life_heatmap.html
```

Add `--skip-geocode` if every row already has `lat`/`lon` filled in (otherwise addresses are resolved via OpenStreetMap Nominatim and the coordinates are cached back into the CSV).

## Your data stays yours

`data/locations.csv` (your real places) and `output/` (the maps generated from them) are both **git-ignored** — see `.gitignore`. The only location data tracked in this repo is `data/locations.example.csv`, a small synthetic dataset used to generate the demo image above. If you fork or clone this project, your own places never get committed unless you deliberately remove them from `.gitignore`.

## Project structure

```
heatmap.py                  Core model: familiarity math, density grid, PNG/HTML rendering, CLI
app.py                      Flask backend for the interactive collector
templates/index.html        Click-to-add map UI (Leaflet)
data/locations.example.csv  Synthetic demo dataset (tracked)
data/locations.csv          Your real places (git-ignored, created on first save)
output/                     Generated heatmaps (git-ignored)
docs/demo_heatmap.png       README screenshot, built from the example dataset
```

## Using this for another city

Everything here is Tehran-flavored by default (`TEHRAN_CENTER` in `heatmap.py`), but nothing in the model is Tehran-specific. Change that one constant to re-center the map anywhere else.

## Requirements

Python 3.10+, and the packages in `requirements.txt` (Flask, pandas, numpy, folium, matplotlib, geopy).

## License

MIT — see [LICENSE](LICENSE).
