"""
Interactive collector for the Tehran life-heatmap.

Click the map, say how you know the place (lived / frequented / traveled / visited / guest) and for
how long, watch the scientifically-scaled familiarity radius draw itself
live, save it -- then generate the full heatmap without leaving the page.

Run:
    .venv/bin/python app.py
Then open http://127.0.0.1:5000
"""

from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from geopy.geocoders import Nominatim

import heatmap as hm

BASE_DIR = Path(__file__).parent
DATA_PATH = BASE_DIR / "data" / "locations.csv"
OUTPUT_DIR = BASE_DIR / "output"
CSV_COLUMNS = ["name", "address", "lat", "lon", "category", "duration_value", "duration_unit", "notes"]

app = Flask(__name__)
_geolocator = Nominatim(user_agent="life-heatmap-tehran-collector")


def load_df() -> pd.DataFrame:
    if not DATA_PATH.exists():
        return pd.DataFrame(columns=CSV_COLUMNS)
    df = pd.read_csv(DATA_PATH, dtype={"address": str, "notes": str})
    for col in CSV_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["address"] = df["address"].fillna("")
    df["notes"] = df["notes"].fillna("")
    return df[CSV_COLUMNS].reset_index(drop=True)


def save_df(df: pd.DataFrame):
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DATA_PATH, index=False)


def row_to_dict(idx, row) -> dict:
    sigma_m, intensity = hm.familiarity(row["category"], row["duration_value"], row["duration_unit"])
    return {
        "id": int(idx),
        "name": row["name"],
        "address": row["address"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "category": row["category"],
        "duration_value": float(row["duration_value"]),
        "duration_unit": row["duration_unit"],
        "notes": row["notes"],
        "sigma_m": round(sigma_m),
        "intensity": round(intensity, 3),
    }


@app.route("/")
def index():
    return render_template(
        "index.html",
        center_lat=hm.TEHRAN_CENTER[0],
        center_lon=hm.TEHRAN_CENTER[1],
        categories=list(hm.CATEGORY_COLOR.items()),
    )


@app.get("/api/locations")
def list_locations():
    df = load_df()
    return jsonify([row_to_dict(idx, row) for idx, row in df.iterrows()])


@app.post("/api/locations")
def add_location():
    body = request.get_json(force=True) or {}
    required = ["name", "lat", "lon", "category", "duration_value", "duration_unit"]
    missing = [f for f in required if body.get(f) in (None, "")]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400
    try:
        hm.familiarity(body["category"], body["duration_value"], body["duration_unit"])
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    df = load_df()
    new_row = {col: body.get(col, "") for col in CSV_COLUMNS}
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    save_df(df)

    idx = len(df) - 1
    return jsonify(row_to_dict(idx, df.loc[idx])), 201


@app.delete("/api/locations/<int:loc_id>")
def delete_location(loc_id):
    df = load_df()
    if loc_id not in df.index:
        return jsonify({"error": "not found"}), 404
    df = df.drop(index=loc_id).reset_index(drop=True)
    save_df(df)
    return "", 204


@app.post("/api/preview")
def preview():
    body = request.get_json(force=True) or {}
    try:
        sigma_m, intensity = hm.familiarity(body["category"], body["duration_value"], body["duration_unit"])
    except (ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"sigma_m": round(sigma_m), "intensity": round(intensity, 3)})


@app.get("/api/reverse_geocode")
def reverse_geocode():
    lat, lon = request.args.get("lat"), request.args.get("lon")
    try:
        location = _geolocator.reverse((float(lat), float(lon)), language="en", timeout=5)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"address": location.address if location else ""})


@app.post("/api/generate")
def generate():
    df = load_df()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    if df.empty:
        return jsonify({"error": "no locations yet"}), 400

    df = hm.compute_weights(df)
    intensity_grid, bounds = hm.build_density_grid(df)
    OUTPUT_DIR.mkdir(exist_ok=True)
    png_path = OUTPUT_DIR / "tehran_life_heatmap.png"
    html_path = OUTPUT_DIR / "tehran_life_heatmap.html"
    hm.render_overlay_png(intensity_grid, png_path)
    hm.build_map(df, png_path, bounds, html_path)

    south, west, north, east = bounds
    return jsonify({
        "image_url": f"/output/{png_path.name}?t={int(png_path.stat().st_mtime)}",
        "bounds": [[south, west], [north, east]],
        "html_url": f"/output/{html_path.name}",
    })


@app.get("/api/share_image")
def share_image():
    df = load_df()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    if df.empty:
        return jsonify({"error": "no locations yet"}), 400

    df = hm.compute_weights(df)
    intensity_grid, bounds = hm.build_density_grid(df)
    OUTPUT_DIR.mkdir(exist_ok=True)
    overlay_path = OUTPUT_DIR / "tehran_life_heatmap.png"
    hm.render_overlay_png(intensity_grid, overlay_path)

    share_path = OUTPUT_DIR / "tehran_life_heatmap_share.png"
    try:
        hm.render_share_image(df, bounds, overlay_path, share_path)
    except Exception as exc:
        return jsonify({"error": f"could not render share image ({exc})"}), 502

    return send_file(share_path, mimetype="image/png", as_attachment=True,
                      download_name="tehran_life_heatmap_share.png")


@app.get("/output/<path:filename>")
def output_file(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
