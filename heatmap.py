"""
Life heatmap over Tehran.

Turns a list of places you've lived, visited, or briefly stopped by into an
interactive map where each point glows with a radius and intensity scaled to
how much time (and what kind of relationship) you had with that place.

Usage:
    .venv/bin/python heatmap.py
    .venv/bin/python heatmap.py --input data/locations.csv --output output/tehran_life_heatmap.html
"""

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import folium
import matplotlib
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

TEHRAN_CENTER = (35.6892, 51.3890)

HOURS_PER_UNIT = {
    "hours": 1,
    "days": 24,
    "months": 24 * 30,
    "years": 24 * 365,
}

# Familiarity grows fast at first, then plateaus with more exposure -- a
# saturating exponential rather than open-ended growth, which is the shape
# spatial-cognition research uses for how people build a "cognitive map" of a
# place (Golledge et al.: landmark -> route -> survey knowledge, acquired
# quickly then diminishing). Where you plateau, both in radius and in how
# saturated the color gets, depends on *how* you're exposed to a place:
#
# - lived: you walk the neighborhood on foot, so the radius grows toward a
#   pedestrian-catchment ("pedshed") scale -- urban planning commonly uses
#   ~5-20 min walk (roughly 400m-1200m) as the radius people become
#   genuinely familiar with around home. tau ~1 year: most of that home
#   range is explored within the first year of living somewhere.
# - frequented: a single anchor point you returned to on a routine basis
#   over an extended stretch of time -- school, a language institute, a
#   job -- without living there. You still wander a bit around it (lunch
#   spots, the walk in), so the radius is wider than a casual repeat visit,
#   but capped well below "lived" since you were only ever there part of
#   the day. tau ~10 months, roughly a school-year rhythm.
# - visited (repeat trips to one destination -- a friend's place, a regular
#   cafe): you go point-to-point rather than wandering, so the radius stays
#   much narrower (the block, not the neighborhood) and saturates faster.
# - traveled (a short trip somewhere outside your routine -- a few days to
#   a couple weeks): unlike "guest" you're actively out sightseeing rather
#   than staying in one venue, so the radius covers real ground -- more
#   than a repeat local visit even. But it's temporary and one-off, so it
#   saturates fast (tau ~2 days, the trip itself) and the color never gets
#   as deep as a relationship you keep returning to.
# - guest (a one-off visit of a few hours): familiarity never leaves the
#   venue itself; tau is hours, not months, since a single visit is all the
#   exposure there is.
#
# These are an informed heuristic, not a measured constant -- tune freely.
CATEGORY_PARAMS = {
    "lived":      {"r_min": 250, "r_max": 1200, "tau_hours": 24 * 365, "i_min": 0.65, "i_max": 0.97},
    "frequented": {"r_min": 150, "r_max": 700,  "tau_hours": 24 * 300, "i_min": 0.50, "i_max": 0.85},
    "traveled":   {"r_min": 150, "r_max": 500,  "tau_hours": 24 * 2,   "i_min": 0.35, "i_max": 0.65},
    "visited":    {"r_min": 80,  "r_max": 350,  "tau_hours": 24 * 180, "i_min": 0.45, "i_max": 0.78},
    "guest":      {"r_min": 40,  "r_max": 120,  "tau_hours": 6,        "i_min": 0.30, "i_max": 0.48},
}

# Colors used both for the popup hit-target radius and (indirectly, via the
# category contrast) how visible each category stays once zoomed out.
CATEGORY_COLOR = {
    "lived": "#d7301f",
    "frequented": "#2b8cbe",
    "traveled": "#41ab5d",
    "visited": "#fc8d59",
    "guest": "#8a8a8a",
}

GRID_RESOLUTION = 450
ALPHA_GAMMA = 0.6   # <1 boosts faint areas so low-familiarity spots stay visible when zoomed out
ALPHA_FLOOR = 0.03  # intensities below this render fully transparent


def familiarity(category: str, duration_value: float, duration_unit: str) -> tuple[float, float]:
    """Absolute (dataset-independent) radius in meters and intensity 0-1
    for one location, given how you were exposed to it and for how long."""
    category = category.strip().lower()
    duration_unit = duration_unit.strip().lower()
    if category not in CATEGORY_PARAMS:
        raise ValueError(f"unknown category '{category}' (expected lived/frequented/traveled/visited/guest)")
    if duration_unit not in HOURS_PER_UNIT:
        raise ValueError(f"unknown duration_unit '{duration_unit}' (expected hours/days/months/years)")

    params = CATEGORY_PARAMS[category]
    hours = max(float(duration_value), 0) * HOURS_PER_UNIT[duration_unit]
    saturation = 1 - np.exp(-hours / params["tau_hours"])

    sigma_m = params["r_min"] + (params["r_max"] - params["r_min"]) * saturation
    intensity = params["i_min"] + (params["i_max"] - params["i_min"]) * saturation
    return sigma_m, intensity


def geocode_missing(df: pd.DataFrame) -> pd.DataFrame:
    missing = df["lat"].isna() | df["lon"].isna()
    if not missing.any():
        return df

    geolocator = Nominatim(user_agent="life-heatmap-tehran")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1)

    for idx in df[missing].index:
        address = df.loc[idx, "address"]
        if not isinstance(address, str) or not address.strip():
            print(f"  [skip] row {idx} ('{df.loc[idx, 'name']}') has no address or lat/lon")
            continue
        print(f"  geocoding: {address}")
        location = geocode(address)
        if location is None:
            print(f"  [warn] could not geocode '{address}' -- fill lat/lon manually")
            continue
        df.loc[idx, "lat"] = location.latitude
        df.loc[idx, "lon"] = location.longitude
        time.sleep(0.5)

    return df


def project_meters(lat, lon, lat0, lon0):
    """Simple equirectangular projection, accurate enough at city scale."""
    x = (lon - lon0) * 111_320 * np.cos(np.radians(lat0))
    y = (lat - lat0) * 110_540
    return x, y


def unproject_meters(x, y, lat0, lon0):
    lon = x / (111_320 * np.cos(np.radians(lat0))) + lon0
    lat = y / 110_540 + lat0
    return lat, lon


def compute_weights(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    computed = df.apply(
        lambda row: familiarity(row["category"], row["duration_value"], row["duration_unit"]),
        axis=1,
        result_type="expand",
    )
    df["sigma_m"] = computed[0]
    df["intensity"] = computed[1]
    return df


def build_density_grid(df: pd.DataFrame):
    lat0, lon0 = TEHRAN_CENTER
    px, py = project_meters(df["lat"].values, df["lon"].values, lat0, lon0)

    pad = df["sigma_m"].max() * 3.5
    x_min, x_max = px.min() - pad, px.max() + pad
    y_min, y_max = py.min() - pad, py.max() + pad

    xs = np.linspace(x_min, x_max, GRID_RESOLUTION)
    ys = np.linspace(y_min, y_max, GRID_RESOLUTION)
    gx, gy = np.meshgrid(xs, ys)

    intensity_grid = np.zeros_like(gx)
    for x, y, sigma, weight in zip(px, py, df["sigma_m"], df["intensity"]):
        d2 = (gx - x) ** 2 + (gy - y) ** 2
        intensity_grid += weight * np.exp(-d2 / (2 * sigma ** 2))

    intensity_grid = np.clip(intensity_grid / intensity_grid.max(), 0, 1)

    south, north = unproject_meters(0, y_min, lat0, lon0)[0], unproject_meters(0, y_max, lat0, lon0)[0]
    west, east = unproject_meters(x_min, 0, lat0, lon0)[1], unproject_meters(x_max, 0, lat0, lon0)[1]

    return intensity_grid, (south, west, north, east)


def render_overlay_png(intensity_grid: np.ndarray, out_path: Path):
    cmap = matplotlib.colormaps["YlOrRd"]
    rgba = cmap(intensity_grid ** ALPHA_GAMMA)
    alpha = intensity_grid ** ALPHA_GAMMA
    alpha[intensity_grid < ALPHA_FLOOR] = 0
    rgba[..., 3] = alpha

    # image row 0 must be the NORTH edge for folium's ImageOverlay
    rgba = np.flipud(rgba)

    from matplotlib import pyplot as plt
    plt.imsave(out_path, rgba)


def build_map(df: pd.DataFrame, overlay_png: Path, bounds, out_html: Path):
    south, west, north, east = bounds
    m = folium.Map(location=TEHRAN_CENTER, zoom_start=12, tiles="CartoDB positron")

    folium.raster_layers.ImageOverlay(
        image=str(overlay_png),
        bounds=[[south, west], [north, east]],
        opacity=0.92,
        interactive=False,
        cross_origin=False,
    ).add_to(m)

    for _, row in df.iterrows():
        popup = (
            f"<b>{row['name']}</b><br>"
            f"{row['category'].title()} &mdash; {row['duration_value']} {row['duration_unit']}<br>"
            f"{row['notes'] if pd.notna(row.get('notes')) else ''}"
        )
        # Invisible hit-target: keeps each spot clickable for its popup
        # without drawing a dot on top of the gradient.
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=10,
            weight=0,
            fill=True,
            fill_opacity=0.001,
            popup=folium.Popup(popup, max_width=250),
        ).add_to(m)

    m.save(str(out_html))


def main():
    parser = argparse.ArgumentParser(description="Build a familiarity heatmap of Tehran.")
    parser.add_argument("--input", default="data/locations.csv")
    parser.add_argument("--output", default="output/tehran_life_heatmap.html")
    parser.add_argument("--skip-geocode", action="store_true", help="don't call Nominatim for missing coords")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_html = Path(args.output)
    output_png = output_html.with_suffix(".png")
    output_html.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_path)
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")

    if not args.skip_geocode:
        print("Geocoding rows missing lat/lon (uses OpenStreetMap Nominatim)...")
        df = geocode_missing(df)
        df.to_csv(input_path, index=False)  # cache resolved coordinates back to disk
        print(f"Updated coordinates saved to {input_path}")

    df = df.dropna(subset=["lat", "lon"])
    if df.empty:
        raise SystemExit("No rows with valid coordinates -- fill in lat/lon or address in the CSV.")

    df = compute_weights(df)
    intensity_grid, bounds = build_density_grid(df)
    render_overlay_png(intensity_grid, output_png)
    build_map(df, output_png, bounds, output_html)

    print(f"\nDone. {len(df)} locations plotted.")
    print(f"Heatmap image: {output_png}")
    print(f"Interactive map: {output_html}")


if __name__ == "__main__":
    main()
