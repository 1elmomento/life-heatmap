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
import concurrent.futures
import io
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import folium
import matplotlib
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

TEHRAN_CENTER = (35.6892, 51.3890)

BASE_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
REFERENCE_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"
SHARE_CARD_BG = "#efe0c4"
SHARE_TITLE_COLOR = "#2b2622"
SHARE_BODY_COLOR = "#8a5a2b"
TILE_CACHE_DIR = Path(__file__).resolve().parent / ".tile_cache"

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


def _deg_to_tile(lat: float, lon: float, zoom: int):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _choose_tile_zoom(bounds, max_tiles: int = 180, max_zoom: int = 16) -> int:
    south, west, north, east = bounds
    for zoom in range(max_zoom, 0, -1):
        x0, y0 = _deg_to_tile(north, west, zoom)
        x1, y1 = _deg_to_tile(south, east, zoom)
        n_tiles = (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
        if n_tiles <= max_tiles:
            return zoom
    return 1


def _fetch_tile_bytes(url: str, cache_path: Path) -> bytes:
    if cache_path.exists():
        return cache_path.read_bytes()
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(resp.content)
    return resp.content


def _fetch_tile_mosaic(bounds, tile_url: str, layer_name: str, zoom: int, tile_size: int = 256):
    """Fetch and stitch the map tiles covering `bounds`, then crop precisely
    to it -- the same tiles the live Leaflet map already renders, just
    assembled server-side into one static image. Tiles are fetched
    concurrently (the network round-trip, not bandwidth, is what makes this
    slow) and cached to disk, so re-rendering the same extent is instant."""
    from PIL import Image

    south, west, north, east = bounds
    x0f, y0f = _deg_to_tile(north, west, zoom)
    x1f, y1f = _deg_to_tile(south, east, zoom)
    x0, x1 = int(math.floor(min(x0f, x1f))), int(math.floor(max(x0f, x1f)))
    y0, y1 = int(math.floor(min(y0f, y1f))), int(math.floor(max(y0f, y1f)))

    def fetch_one(coord):
        xt, yt = coord
        cache_path = TILE_CACHE_DIR / layer_name / str(zoom) / f"{xt}_{yt}.tile"
        try:
            data = _fetch_tile_bytes(tile_url.format(z=zoom, x=xt, y=yt), cache_path)
            return coord, Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            return coord, Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))

    coords = [(xt, yt) for xt in range(x0, x1 + 1) for yt in range(y0, y1 + 1)]
    mosaic = Image.new("RGBA", ((x1 - x0 + 1) * tile_size, (y1 - y0 + 1) * tile_size))
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for (xt, yt), tile_img in pool.map(fetch_one, coords):
            mosaic.paste(tile_img, ((xt - x0) * tile_size, (yt - y0) * tile_size))

    crop_box = (
        (x0f - x0) * tile_size, (y0f - y0) * tile_size,
        (x1f - x0) * tile_size, (y1f - y0) * tile_size,
    )
    return mosaic.crop(crop_box)


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_share_image(df: pd.DataFrame, bounds, overlay_png: Path, out_path: Path,
                        title: str = "My Life Familiarity Map — Tehran", description: str | None = None):
    """A single self-contained, high-resolution PNG (basemap + heat glow +
    title/description/legend caption) meant for sharing outside the app --
    social media, messaging, printing -- where an interactive Leaflet page
    doesn't work."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    zoom = _choose_tile_zoom(bounds)
    base = _fetch_tile_mosaic(bounds, BASE_TILE_URL, "base", zoom)
    reference = _fetch_tile_mosaic(bounds, REFERENCE_TILE_URL, "reference", zoom)
    basemap = Image.alpha_composite(base, reference)

    overlay = Image.open(overlay_png).convert("RGBA").resize(basemap.size, Image.LANCZOS)
    combined = Image.alpha_composite(basemap, overlay).convert("RGB")

    # Upscale small extents so the export still reads as "HQ" at social-media sizes.
    min_width = 1600
    if combined.width < min_width:
        scale = min_width / combined.width
        combined = combined.resize((round(combined.width * scale), round(combined.height * scale)), Image.LANCZOS)

    map_w, map_h = combined.size
    font_dir = Path(matplotlib.get_data_path()) / "fonts" / "ttf"

    # Attribution sits in the map's top-right corner (Esri's free tiles
    # require it to stay visible somewhere) -- kept up there, away from the
    # caption card, so the two never fight for space.
    draw = ImageDraw.Draw(combined, "RGBA")
    attr_font = ImageFont.truetype(str(font_dir / "DejaVuSans.ttf"), size=round(map_w * 0.011))
    attribution = "Esri, HERE, Garmin, OpenStreetMap contributors"
    attr_pad = round(map_w * 0.01)
    attr_h = round(map_w * 0.026)
    attr_w = draw.textlength(attribution, font=attr_font)
    draw.rectangle([map_w - attr_w - 2 * attr_pad, 0, map_w, attr_h], fill=(255, 255, 255, 170))
    draw.text((map_w - attr_w - attr_pad, round(attr_h * 0.28)), attribution, font=attr_font, fill="#4a463f")

    # The caption is a full-width, square-edged bar that overlaps the
    # bottom edge of the map slightly, sized to fit its own text (title +
    # up to two description lines).
    inner_pad = round(map_w * 0.045)

    title_font = ImageFont.truetype(str(font_dir / "DejaVuSerif-Bold.ttf"), size=round(map_w * 0.034))
    body_font = ImageFont.truetype(str(font_dir / "DejaVuSerif.ttf"), size=round(map_w * 0.022))

    dummy_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_text_w = map_w - 2 * inner_pad
    while title_font.size > 14 and dummy_draw.textlength(title, font=title_font) > max_text_w:
        title_font = ImageFont.truetype(str(font_dir / "DejaVuSerif-Bold.ttf"), size=title_font.size - 2)

    if description is None:
        description = f"{len(df)} places I've lived, worked, traveled to, and visited around Tehran."
    desc_lines = _wrap_text(dummy_draw, description, body_font, max_text_w)[:2]

    title_h = round(title_font.size * 1.3)
    line_h = round(body_font.size * 1.4)
    gap = round(map_w * 0.014)
    content_h = title_h + gap + len(desc_lines) * line_h
    card_h = content_h + 2 * inner_pad
    overlap = round(card_h * 0.3)

    canvas = Image.new("RGB", (map_w, map_h + card_h - overlap), SHARE_CARD_BG)
    canvas.paste(combined, (0, 0))
    card_box = (0, map_h - overlap, map_w, map_h - overlap + card_h)

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(
        [0, card_box[1] + round(map_w * 0.01), map_w, card_box[1] + round(map_w * 0.05)],
        fill=(0, 0, 0, 100),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(map_w * 0.015)))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), shadow).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    draw.rectangle(card_box, fill=SHARE_CARD_BG)

    text_left = inner_pad
    y = card_box[1] + inner_pad
    draw.text((text_left, y), title, font=title_font, fill=SHARE_TITLE_COLOR)
    y += title_h + gap
    for line in desc_lines:
        draw.text((text_left, y), line, font=body_font, fill=SHARE_BODY_COLOR)
        y += line_h

    canvas.save(out_path)


def build_map(df: pd.DataFrame, overlay_png: Path, bounds, out_html: Path):
    south, west, north, east = bounds
    m = folium.Map(
        location=TEHRAN_CENTER,
        zoom_start=12,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr="Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
        max_zoom=16,
    )
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
        name="labels",
        overlay=True,
        control=False,
        max_zoom=16,
    ).add_to(m)

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

    # "Download shareable image" only works when this page is served by the
    # Flask app (app.py) -- /api/share_image is one of its routes. Opened
    # standalone (double-clicked, no server) the button explains that
    # instead of silently failing.
    m.get_root().html.add_child(folium.Element("""
    <style>
      #share-img-wrap { position: fixed; bottom: 20px; right: 20px; z-index: 9999;
        text-align: right; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }
      #share-img-btn { display: inline-flex; align-items: center; gap: 9px;
        background: #d7301f; color: #fff; border: none; padding: 13px 22px;
        border-radius: 999px; font-size: 14.5px; font-weight: 600; letter-spacing: .2px;
        cursor: pointer; box-shadow: 0 4px 14px rgba(215,48,31,.4);
        transition: transform .15s ease, box-shadow .15s ease, background .15s ease; }
      #share-img-btn:hover:not(:disabled) { background: #c22a1c;
        box-shadow: 0 6px 18px rgba(215,48,31,.5); transform: translateY(-1px); }
      #share-img-btn:active:not(:disabled) { transform: translateY(0); }
      #share-img-btn:disabled { background: #b0aaa2; box-shadow: none; cursor: default; }
      #share-img-btn .spinner { width: 14px; height: 14px; border-radius: 50%;
        border: 2px solid rgba(255,255,255,.4); border-top-color: #fff;
        animation: share-spin .7s linear infinite; display: none; }
      #share-img-btn.loading .spinner { display: inline-block; }
      #share-img-btn.loading .icon { display: none; }
      @keyframes share-spin { to { transform: rotate(360deg); } }
      #share-img-status { display: none; margin-top: 8px; font-size: 12.5px; line-height: 1.4;
        max-width: 260px; color: #2b2622; background: #fffaf3; padding: 8px 12px;
        border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.15); text-align: left; }
    </style>
    <div id="share-img-wrap">
      <button id="share-img-btn">
        <span class="icon">\U0001F4F8</span><span class="spinner"></span>
        <span class="label">Download shareable image</span>
      </button>
      <div id="share-img-status"></div>
    </div>
    <script>
    document.getElementById('share-img-btn').addEventListener('click', async () => {
      const btn = document.getElementById('share-img-btn');
      const label = btn.querySelector('.label');
      const status = document.getElementById('share-img-status');
      btn.disabled = true;
      btn.classList.add('loading');
      label.textContent = 'Rendering…';
      status.style.display = 'none';
      try {
        const res = await fetch('/api/share_image');
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || 'server error');
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'tehran_life_heatmap_share.png';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      } catch (e) {
        status.textContent = 'Could not reach the app server — run "python app.py" and open this page from there (not as a local file) to use this button.';
        status.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
        label.textContent = 'Download shareable image';
      }
    });
    </script>
    """))

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
