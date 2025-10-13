"""
Flask blueprint: /reports
Serves generated analysis outputs (Excel, CSV, PNG charts) directly from /results.
"""

from flask import Blueprint, render_template, send_from_directory, abort
from pathlib import Path
import datetime

reports_bp = Blueprint(
    "reports",
    __name__,
    template_folder="templates",  # point Flask to our new templates folder
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "results"


@reports_bp.route("/")
def index():
    if not RESULTS_DIR.exists():
        abort(404, "Results folder not found.")
    files = sorted(
        RESULTS_DIR.glob("*.*"),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )

    file_info = []
    for f in files:
        st = f.stat()
        file_info.append({
            "name": f.name,
            "suffix": f.suffix,
            "size": round(st.st_size / 1024, 1),
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })

    images = [f for f in RESULTS_DIR.glob("*.png")]
    return render_template("reports_index.html", files=file_info, images=images)


@reports_bp.route("/file/<path:filename>")
def serve_file(filename):
    path = RESULTS_DIR / filename
    if not path.exists():
        abort(404)
    return send_from_directory(RESULTS_DIR, filename, as_attachment=False)


@reports_bp.route("/api/list")
def list_reports_json():
    """Return JSON metadata for available reports."""
    if not RESULTS_DIR.exists():
        return {"error": "results folder not found"}, 404

    data = []
    for f in RESULTS_DIR.glob("*.*"):
        st = f.stat()
        data.append({
            "name": f.name,
            "suffix": f.suffix,
            "size_kb": round(st.st_size / 1024, 1),
            "modified": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    data.sort(key=lambda x: x["modified"], reverse=True)
    return data
