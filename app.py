
import logging
import os
import numpy as np
import math
from datetime import datetime
from flask import Flask, request, render_template, jsonify, send_file, Response, current_app, Blueprint, redirect, url_for, flash
from auth_db import SessionAuth, User
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from sqlalchemy import select, and_, text
from sqlalchemy.orm import joinedload
from sqlalchemy.dialects import mysql  # For SQL logging
from models import (
    SessionLocal,
    StarCatalog,
    Frame,
    Astrometry,
    CalFrameQuality,
    FrameQuality,
)
from mywcs import create_simple_wcs
from astropy.wcs import NoConvergence
from astropy.io import fits
from astropy.io import fits as afits
from io import StringIO, BytesIO
import csv
from astropy.table import Table

# Base directory where  RED FITS sub-folders live
FITS_ROOT = "/nfs/php2/ar3/P/HP1/REDUCTION/RED"
SUB_ROOT = "/nfs/php2/ar3/P/HP1/REDUCTION/SUB"

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]

app.config.update(
    SESSION_COOKIE_SECURE   = True,   # HTTPS only
    SESSION_COOKIE_HTTPONLY = True,   # JS can’t read it
    SESSION_COOKIE_SAMESITE = "Lax",  # blocks most CSRF
)

# -----------------------------------------------------------------------------
# Logging configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)



login_manager = LoginManager()
login_manager.login_view = "auth.login"      # redirect target
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    db = SessionAuth()
    return db.query(User).get(int(user_id))



auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    # ------------------------------------------------------------------
    # POST  →  handle the submitted form from the modal
    # ------------------------------------------------------------------
    if request.method == "POST":
        
        app.logger.info("RAW POST ⇒ %s", request.form.to_dict(flat=False))
        
        name     = request.form.get("name")
        email    = request.form["email"].lower()
        password = request.form["password"]
        
        wants_notif = "notifications" in request.form
        app.logger.info("wants_notif computed ⇒ %s", wants_notif)   # ← 1️⃣


        db = SessionAuth()

        # 1.  Duplicate-email check
        if db.query(User).filter_by(email=email).first():
            flash("Email already registered")
            return redirect(url_for("frames_page"))

        # 2.  Basic password length
        if len(password) < 8:         # ← adjust if you want 8+
            flash("Use a longer password")
            return redirect(url_for("frames_page"))

        # 3.  Create the record
        user = User.create(db, name, email, password, notifications=wants_notif)
        app.logger.info("DB says notifications stored ⇒ %s", user.notifications)  # ← 2️⃣

        # 4.  Auto-log-in & success message
        login_user(user, remember=True)                # <— signed in now
        flash("Account created — you’re now logged in!")

        return redirect(url_for("frames_page"))        # <— go to /data

    # ------------------------------------------------------------------
    # GET  →  fallback (rarely used; modal handles normal flow)
    # ------------------------------------------------------------------
    return render_template("register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].lower()
        password = request.form["password"]

        app.logger.info("Login attempt for %s", email)

        db = SessionAuth()
        user = db.query(User).filter_by(email=email).first()
        if user and user.verify_password(password):

            app.logger.info("Password OK — logging user in")

            login_user(user, remember=True)        # sets secure session cookie
            next_page = request.args.get("next") or url_for("frames_page")
            return redirect(next_page)
        flash("Invalid credentials")
    return render_template("login.html")

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("frames_page"))


def paginate(seq, page, per_page):
    total_pages = max(1, (len(seq) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return seq[start:start + per_page], total_pages, page


def query_lightcurve_path(gaia_id: str):
    session = SessionLocal()
    try:
        sql = text("""
            SELECT path_to_file
            FROM   HPLC.stitched_lightcurve_files
            WHERE  Gaia_DR2_ID = :gid
            LIMIT  1
        """)
        return session.execute(sql, {"gid": gaia_id}).scalar()   # None if not found
    finally:
        session.close()

# --------------------------------------------------------------------------
#  Return (path, time[], mag[])   None, None, None → not found / no data
# --------------------------------------------------------------------------
def load_lightcurve_arrays(gaia_id: str):
    session = SessionLocal()
    try:
        path = session.execute(
            text("""
                SELECT path_to_file
                FROM   HPLC.stitched_lightcurve_files
                WHERE  Gaia_DR2_ID = :gid
                LIMIT  1
            """),
            {"gid": gaia_id}
        ).scalar()
    finally:
        session.close()

    if path is None or not os.path.isfile(path):
        return None, None, None

    # ---------- FITS read --------------------------------------------------
    with fits.open(path, memmap=False) as hdul:
        tab = hdul[1].data
        names = [n.upper() for n in tab.names]

        # --- TIME column ---------------------------------------------------
        if "TIME" in names:
            tcol = "TIME"
        elif "BTJD" in names:
            tcol = "BTJD"
        elif "JD" in names:
            tcol = "JD"
        else:
            return path, [], []          # no time column → give up

        # --- Magnitude column ---------------------------------------------
        mag_candidates = ("FITMAG0", "MAG", "TFA0", "SAP_MAG")
        mcol = next((c for c in mag_candidates if c.upper() in names), None)
        if mcol is None:
            return path, [], []          # no magnitude column either

        time = np.asarray(tab[tcol]).astype(float).tolist()
        mag  = np.asarray(tab[mcol]).astype(float).tolist()

    return path, time, mag



# -----------------------------------------------------------------------------
# Utility: check if a world coordinate is on the CCD via WCS
# -----------------------------------------------------------------------------
def check_coordinate_on_ccd(ra_deg, dec_deg, wcs, margin=0, extent=(0, 2048, 0, 2048)):
    """Return True if (ra_deg, dec_deg) maps inside the CCD bounds."""
    try:
        xpix, ypix = wcs.all_world2pix(ra_deg, dec_deg, 1)
    except NoConvergence:
        return False

    if np.isnan(xpix) or np.isnan(ypix):
        return False

    return (
        (extent[0] - margin) < xpix < (extent[1] + margin)
        and (extent[2] - margin) < ypix < (extent[3] + margin)
    )

# -----------------------------------------------------------------------------
# Stage 1: find candidate fields via simple WCS projection
# -----------------------------------------------------------------------------
def query_fields_by_coordinate(ra_deg, dec_deg, margin=100, extent=(0, 2048, 0, 2048),
                               crpix=(1024, 1024), pixsize=19.62):
    """
    Returns a list of StarCatalog.OBJECT names whose approximate TAN
    projection might contain (ra_deg, dec_deg).
    """
    session = SessionLocal()
    try:
        stmt = (
            select(StarCatalog.OBJECT, StarCatalog.RA, StarCatalog.DEC)
            .group_by(StarCatalog.OBJECT)
        )
        rows = session.execute(stmt).all()
    finally:
        session.close()

    fields = []
    for obj_name, cat_ra, cat_dec in rows:
        w_approx = create_simple_wcs((cat_ra, cat_dec), crpix=crpix, pixsize=pixsize)
        if check_coordinate_on_ccd(ra_deg, dec_deg, w_approx, margin=margin):
            fields.append(obj_name)
    return fields

# -----------------------------------------------------------------------------
# Stage 2: full database query + on-CCD filtering
# -----------------------------------------------------------------------------
def query_frames_by_coordinate(ra_deg, dec_deg,
                               date_min=None, date_max=None,
                               date_type="datetime",
                               margin=100, extent=(0, 2048, 0, 2048)):
    """
    1) Use query_fields_by_coordinate to shortlist fields.
    2) Query Frame ⟶ Astrometry ⟶ CalFrameQuality & FrameQuality for sky_bg, moondist, sunelev.
    3) Do precise on-CCD check using full WCS.
    Returns a list of dicts with all needed attributes.
    """
    fields = query_fields_by_coordinate(ra_deg, dec_deg, margin=margin, extent=extent)
    app.logger.info(f"Candidate fields: {fields}")
    if not fields:
        return []

    session = SessionLocal()
    try:
        stmt = (
            select(
                Frame,
                CalFrameQuality.calframe_median.label("sky_bg"),
                FrameQuality.MOONDIST.label("moondist"),
                FrameQuality.SUNELEV.label("sunelev"),
            )
            .options(joinedload(Frame.astrometry))
            .join(Frame.astrometry)
            .outerjoin(
                CalFrameQuality,
                and_(
                    Frame.IHUID == CalFrameQuality.IHUID,
                    Frame.FNUM == CalFrameQuality.FNUM,
                ),
            )
            .outerjoin(
                FrameQuality,
                and_(
                    Frame.IHUID == FrameQuality.IHUID,
                    Frame.FNUM == FrameQuality.FNUM,
                ),
            )
            .where(Frame.OBJECT.in_(fields))
            .where(Astrometry.exit_code == 0)
        )

        # Date filters
        app.logger.info(f"Date filter inputs => type={date_type}, min={date_min}, max={date_max}")
        if date_type == "datetime":
            if date_min is not None:
                stmt = stmt.where(Frame.datetime_obs >= date_min)
            if date_max is not None:
                stmt = stmt.where(Frame.datetime_obs <= date_max)
        elif date_type == "JD":
            if date_min is not None:
                jdmin = date_min - 2400000
                stmt = stmt.where(Frame.JD >= jdmin)
            if date_max is not None:
                jdmax = date_max - 2400000
                stmt = stmt.where(Frame.JD <= jdmax)

        # Log SQL for debugging
        compiled = stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
        app.logger.info(f"SQL Query:\n{compiled}")

        rows = session.execute(stmt).all()
    finally:
        session.close()

    app.logger.info(f"Found {len(rows)} rows before on-CCD filtering.")

    # Final on-CCD check + build result dicts
    matched = []
    for fr, sky_bg, moondist, sunelev in rows:
        w = fr.astrometry.wcs_transform
        if w is None:
            continue
        if check_coordinate_on_ccd(ra_deg, dec_deg, w, margin=0):
            matched.append({
                "IHUID":        fr.IHUID,
                "FNUM":         fr.FNUM,
                "OBJECT":       fr.OBJECT,
                "IMAGETYP":     (fr.IMAGETYP or "").lower(),
                "datetime_obs": fr.datetime_obs.isoformat() if fr.datetime_obs else None,
                "EXPTIME":      fr.EXPTIME,
                "relpath":      fr.relpath,
                "sky_bg":       sky_bg,
                "moondist":     moondist,
                "sunelev":      sunelev,
            })
    app.logger.info(f"Out of those, {len(matched)} frames are actually on the CCD.")
    return matched





# -----------------------------------------------------------------------------
# HTML search interface – “Frames” view  (/data)
# -----------------------------------------------------------------------------
# >>> BEGIN clean frames_page -------------------------------------------------
# --------------------------------------------------------------------------
#  Shared search logic     (DO NOT make this a route)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
#  Shared search helper used by /data  and  /data/lightcurves
# --------------------------------------------------------------------------
def _run_search(active_page: str, show_upcoming: bool):
    
    app.logger.info("Inside _run_search  |  current_user.is_authenticated = %s",
                    current_user.is_authenticated)
    
    if not current_user.is_authenticated:
        return render_template(
            "lightcurves.html",
            active_page=active_page,
            show_upcoming=show_upcoming
        )
        
    """
    Frames view  (active_page == "frames"):
        • POST expects RA / DEC + optional date range
        • Queries HPCALIB, paginates Object / Twilight frames

    Light-Curves view  (active_page == "lightcurves"):
        • POST expects a GAIA_ID
        • Queries HPLC.stitched_lightcurve_files for path_to_file

    Both views:
        • GET shows an empty form
        • Always pass active_page + show_upcoming to template
    """

    # ======================================================================
    # LIGHT-CURVES  → GAIA-ID search
    # ======================================================================
    if active_page == "lightcurves" and request.method == "POST":
        gaia_id = request.form.get("gaia_id", "").strip()

        if not gaia_id:
            return render_template(
                "lightcurves.html",
                error="Please enter a GAIA ID.",
                active_page=active_page,
                show_upcoming=show_upcoming
            )

        lc_path, lc_time, lc_flux = load_lightcurve_arrays(gaia_id)

        if lc_path is None:
            return render_template(
                "lightcurves.html",
                message="No light curve found for that GAIA ID.",
                active_page=active_page,
                show_upcoming=show_upcoming
            )

        # success → send data lists to template
        return render_template(
            "lightcurves.html",
            lightcurve_path=lc_path,
            lc_time=lc_time,
            lc_flux=lc_flux,
            active_page=active_page,
            show_upcoming=show_upcoming
        )

    # ======================================================================
    # FRAMES  → RA/DEC search (unchanged from original logic)
    # ======================================================================
    if active_page == "frames" and request.method == "POST":

        # 1.  RA / DEC
        ra_str = request.form.get("ra", "").strip()
        dec_str = request.form.get("dec", "").strip()
        try:
            ra = float(ra_str)
            dec = float(dec_str)
        except ValueError:
            return render_template(
                "lightcurves.html",
                frames=[],
                error="Please provide numeric RA and DEC.",
                ra=ra_str, dec=dec_str,
                date_type=request.form.get("date_type", "datetime"),
                date_min_input=request.form.get("date_min", ""),
                date_max_input=request.form.get("date_max", ""),
                active_page=active_page,
                show_upcoming=show_upcoming
            )

        # 2.  Date range
        dt_type = request.form.get("date_type", "datetime").strip()
        dmin_in = request.form.get("date_min", "").strip()
        dmax_in = request.form.get("date_max", "").strip()
        dmin = dmax = None
        try:
            if dt_type == "datetime":
                if dmin_in:
                    dmin = datetime.strptime(dmin_in, "%Y-%m-%d")
                if dmax_in:
                    dmax = datetime.strptime(dmax_in, "%Y-%m-%d")
            else:  # JD
                if dmin_in:
                    dmin = float(dmin_in)
                if dmax_in:
                    dmax = float(dmax_in)
        except ValueError:
            pass  # ignore bad dates → treat as no limit

        # 3.  DB query
        all_frames = query_frames_by_coordinate(
            ra, dec, date_min=dmin, date_max=dmax, date_type=dt_type
        )

        # 4.  Split + paginate
        obj_list = [f for f in all_frames if f.get("IMAGETYP", "").lower() == "object"]
        twl_list = [f for f in all_frames if f.get("IMAGETYP", "").lower() == "twilight"]

        PER_PAGE = 50
        page_obj = int(request.form.get("page_obj", "1") or 1)
        page_twl = int(request.form.get("page_twl", "1") or 1)

        obj_page, obj_pages, page_obj = paginate(obj_list, page_obj, PER_PAGE)
        twl_page, twl_pages, page_twl = paginate(twl_list, page_twl, PER_PAGE)

        if not obj_list and not twl_list:
            return render_template(
                "lightcurves.html",
                frames=None,
                message="No coverage found.",
                ra=ra_str, dec=dec_str,
                date_type=dt_type,
                date_min_input=dmin_in, date_max_input=dmax_in,
                active_page=active_page,
                show_upcoming=show_upcoming
            )

        return render_template(
            "lightcurves.html",
            object_frames=obj_page,
            twilight_frames=twl_page,
            object_total=len(obj_list),
            twilight_total=len(twl_list),
            page_obj=page_obj,   total_pages_obj=obj_pages,
            page_twl=page_twl,   total_pages_twl=twl_pages,
            current_view=request.form.get("view", "object"),
            ra=ra_str, dec=dec_str,
            date_type=dt_type,
            date_min_input=dmin_in, date_max_input=dmax_in,
            active_page=active_page,
            show_upcoming=show_upcoming
        )

    # ======================================================================
    # GET  → empty sidebar (both views)
    # ======================================================================
    return render_template(
        "lightcurves.html",
        active_page=active_page,
        show_upcoming=show_upcoming
    )



# -----------------------------------------------------------------------------
# HTML search interface – *Light Curves* view
# -----------------------------------------------------------------------------
# --------------------------------------------------------------------------
#  /data  →  Frames  (default)
# --------------------------------------------------------------------------
@app.route("/data", methods=["GET", "POST"])
def frames_page():
    return _run_search(active_page="frames", show_upcoming=True)


# --------------------------------------------------------------------------
#  /data/lightcurves  →  Light Curves
# --------------------------------------------------------------------------
@app.route("/data/lightcurves", methods=["GET", "POST"])
def lightcurves_page():
    return _run_search(active_page="lightcurves", show_upcoming=False)



# -----------------------------------------------------------------------------
# Serve FITS files for JS9 viewer
# -----------------------------------------------------------------------------
@app.route("/fits/<string:kind>/<int:ihuid>/<int:fnum>")
def serve_fits(kind: str, ihuid: int, fnum: int):
    """
    Stream a FITS file to JS9.
      kind='red' →  …/RED/.../<base>-red.fits[.fz]
      kind='sub' →  …/SUB/.../<base>-sub.fits[.fz]
    """
    kind = kind.lower().strip()
    if kind not in ("red", "sub"):
        current_app.logger.error("[serve_fits] invalid kind=%s", kind)
        return "Invalid FITS type", 404

    suffix   = f"-{kind}"                  # "-red" or "-sub"
    root_dir = FITS_ROOT if kind == "red" else SUB_ROOT

    current_app.logger.info(
        "[serve_fits] kind=%s ihuid=%d fnum=%d root=%s",
        kind, ihuid, fnum, root_dir
    )

    # 1) Frame lookup ----------------------------------------------------
    session = SessionLocal()
    frame = session.query(Frame).filter_by(IHUID=ihuid, FNUM=fnum).one_or_none()
    session.close()
    if frame is None:
        current_app.logger.error("[serve_fits] No Frame %d/%d", ihuid, fnum)
        return "Frame not found", 404

    # 2) Derive the base filename ---------------------------------------
    base = (frame.frame_name or "").strip()
    # strip compression + .fits
    for ext in (".fits", ".fz"):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
    # strip any existing "-red" or "-sub"
    if base.lower().endswith("-red"):
        base = base[:-4]
    if base.lower().endswith("-sub"):
        base = base[:-4]

    date_dir = (frame.date_dir or "").strip()
    ihu_dir  = f"ihu{frame.IHUID:02d}"
    dirpath  = os.path.join(root_dir, date_dir, ihu_dir)

    current_app.logger.info("[serve_fits] searching %s", dirpath)

    # 3) Candidate filenames --------------------------------------------
    candidates = [f"{base}{suffix}.fits.fz", f"{base}{suffix}.fits"]

    for fname in candidates:
        fullpath = os.path.join(dirpath, fname)
        current_app.logger.debug("[serve_fits] checking %s", fullpath)

        if not os.path.isfile(fullpath):
            continue

        current_app.logger.info("[serve_fits] found %s", fullpath)

        # 4) On-the-fly decompression for .fz ----------------------------
        if fullpath.lower().endswith(".fz"):
            try:
                with afits.open(fullpath, ignore_missing_end=True, memmap=False) as hdul:
                    buf = BytesIO()
                    hdul.writeto(buf, overwrite=True)
                    buf.seek(0)
                    current_app.logger.info(
                        "[serve_fits] decompressed to %d bytes", buf.getbuffer().nbytes
                    )
                    return Response(
                        buf.getvalue(),
                        mimetype="application/fits",
                        headers={"Content-Disposition": f'inline; filename="{fname[:-3]}"'}
                    )
            except Exception as exc:
                current_app.logger.exception("[serve_fits] decompress error")
                return f"Error reading FITS: {exc}", 500

        # 5) Plain .fits – stream directly -------------------------------
        return send_file(fullpath, mimetype="application/fits")

    # 6) Nothing matched -------------------------------------------------
    current_app.logger.error("[serve_fits] none of %s in %s", candidates, dirpath)
    return "FITS file not found", 404



# -----------------------------------------------------------------------------
# Programmatic API endpoint (JSON / CSV / VOTable)
# -----------------------------------------------------------------------------
@app.route('/api/data', methods=['POST'])
def data_api():
    fmt = request.args.get('format', 'json').lower()
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    data = request.get_json()

    # Validate RA/DEC
    try:
        ra = float(data.get('ra', '').strip())
        dec = float(data.get('dec', '').strip())
    except Exception:
        return jsonify({"error": "Invalid RA or DEC"}), 400

    # Validate dates
    dt_type = data.get('date_type', 'datetime').strip()
    dmin_in = data.get('date_min', '').strip()
    dmax_in = data.get('date_max', '').strip()
    dmin = dmax = None
    if dt_type == 'datetime':
        try:
            if dmin_in:
                dmin = datetime.strptime(dmin_in, '%Y-%m-%d')
            if dmax_in:
                dmax = datetime.strptime(dmax_in, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400
    else:
        try:
            if dmin_in:
                dmin = float(dmin_in)
            if dmax_in:
                dmax = float(dmax_in)
        except ValueError:
            return jsonify({"error": "Invalid JD dates"}), 400

    frames = query_frames_by_coordinate(
        ra, dec,
        date_min=dmin, date_max=dmax, date_type=dt_type
    )

    # Build normalized result dicts
    results = []
    for f in frames:
        dt = f.get("datetime_obs")
        if dt and not dt.endswith("Z"):
            dt += "Z"
        results.append({
            "object":             f.get("OBJECT", "").lower(),
            "ihuid":              f.get("IHUID"),
            "fnum":               f.get("FNUM"),
            "datetime_obs":       dt,
            "exptime":            f.get("EXPTIME"),
            "sky_background_adu": f.get("sky_bg"),
            "moon_distance":      f.get("moondist"),
            "sun_elevation":      f.get("sunelev"),
            "download_url":       f"https://hatpi.org/data/{f.get('relpath')}",
        })

    # CSV output
    if fmt == "csv":
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "object","ihuid","fnum","datetime_obs",
            "exptime","sky_background_adu",
            "moon_distance","sun_elevation",
            "download_url"
        ])
        for row in results:
            writer.writerow([
                row["object"], row["ihuid"], row["fnum"],
                row["datetime_obs"], row["exptime"],
                row["sky_background_adu"],
                row["moon_distance"], row["sun_elevation"],
                row["download_url"]
            ])
        return Response(output.getvalue(), mimetype="text/csv")

    # VOTable output
    if fmt == "votable":
        table = Table(rows=results, names=list(results[0].keys()) if results else [])
        buf = BytesIO()
        table.write(buf, format="votable")
        return Response(buf.getvalue(), mimetype="application/x-votable+xml")

    # Default JSON
    return jsonify({"total_frames": len(results), "frames": results}), 200

app.register_blueprint(auth_bp)

# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5002)
    

