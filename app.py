
import logging
import os
import numpy as np
import math
from datetime import datetime
from flask import Flask, request, render_template, jsonify, send_file, Response
from sqlalchemy import select, and_
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
from astropy.io import fits as afits
from io import StringIO, BytesIO
import csv
from astropy.table import Table

# Base directory where your RED FITS sub-folders live
FITS_ROOT = "/nfs/php2/ar3/P/HP1/REDUCTION/RED"

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Logging configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

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
# HTML search interface
# -----------------------------------------------------------------------------
@app.route('/data', methods=['GET', 'POST'])
def lightcurves():
    if request.method == 'POST':
        # Read & validate form inputs
        ra_str = request.form.get('ra', '').strip()
        dec_str = request.form.get('dec', '').strip()
        try:
            ra = float(ra_str)
            dec = float(dec_str)
        except ValueError:
            return render_template(
                'lightcurves.html',
                frames=[],
                error="Please provide valid numeric RA and DEC.",
                ra=ra_str, dec=dec_str,
                date_type=request.form.get('date_type', 'datetime'),
                date_min_input=request.form.get('date_min', ''),
                date_max_input=request.form.get('date_max', '')
            )

        # Parse dates
        dt_type = request.form.get('date_type', 'datetime').strip()
        dmin_in = request.form.get('date_min', '').strip()
        dmax_in = request.form.get('date_max', '').strip()
        dmin = dmax = None
        if dt_type == 'datetime':
            try:
                if dmin_in:
                    dmin = datetime.strptime(dmin_in, '%Y-%m-%d')
                if dmax_in:
                    dmax = datetime.strptime(dmax_in, '%Y-%m-%d')
            except ValueError:
                pass
        else:  # JD
            try:
                if dmin_in:
                    dmin = float(dmin_in)
                if dmax_in:
                    dmax = float(dmax_in)
            except ValueError:
                pass

        # Pagination setup
        page = 1
        try:
            page = int(request.form.get('page', '1'))
        except ValueError:
            pass
        page_size = 50

        # Run query
        all_frames = query_frames_by_coordinate(
            ra, dec,
            date_min=dmin, date_max=dmax, date_type=dt_type
        )
        total = len(all_frames)
        total_pages = max(1, math.ceil(total / page_size))
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        frames_page = all_frames[start:start + page_size]

        if total == 0:
            return render_template(
                'lightcurves.html',
                frames=[], total_count=0,
                message="No coverage found.",
                ra=ra_str, dec=dec_str,
                date_type=dt_type,
                date_min_input=dmin_in, date_max_input=dmax_in,
                page=1, total_pages=1
            )

        return render_template(
            'lightcurves.html',
            frames=frames_page,
            total_count=total,
            page=page,
            total_pages=total_pages,
            ra=ra_str, dec=dec_str,
            date_type=dt_type,
            date_min_input=dmin_in,
            date_max_input=dmax_in
        )

    # GET: just show empty form
    return render_template('lightcurves.html', frames=None)

# -----------------------------------------------------------------------------
# Serve FITS files for JS9 viewer
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Serve RED FITS files for JS9 viewer, with exhaustive logging & both .fz/.fits
# -----------------------------------------------------------------------------
@app.route('/fits/<int:ihuid>/<int:fnum>')
def serve_fits(ihuid, fnum):
    app.logger.info(f"[serve_fits] called with ihuid={ihuid}, fnum={fnum}")

    # 1) Fetch the Frame record
    session = SessionLocal()
    frame = session.query(Frame).filter_by(IHUID=ihuid, FNUM=fnum).one_or_none()
    session.close()
    if frame is None:
        app.logger.error(f"[serve_fits] No Frame record for {ihuid}/{fnum}")
        return "Frame not found", 404

    # 2) Log DB‐side filename info
    app.logger.info(
        f"[serve_fits] frame.date_dir={frame.date_dir}, "
        f"frame.frame_name={frame.frame_name!r}, "
        f"frame.compression={frame.compression!r}"
    )

    # 3) Derive the “base” (strip .fits or .fz if present)
    base = (frame.frame_name or "").strip()
    if base.lower().endswith('.fits'):
        base = base[:-5]
    if base.lower().endswith('.fz'):
        base = base[:-3]
    app.logger.info(f"[serve_fits] base name stripped to {base!r}")

    # 4) Build the directory under FITS_ROOT
    date_dir = (frame.date_dir or "").strip()
    ihu_dir  = f"ihu{frame.IHUID:02d}"
    dirpath  = os.path.join(FITS_ROOT, date_dir, ihu_dir)
    app.logger.info(f"[serve_fits] looking in directory: {dirpath}")

    # 5) Try the two possible filenames ***directly*** in that folder
    candidates = [f"{base}-red.fits.fz", f"{base}-red.fits"]
    app.logger.warning("CANDIDATES = %s",
                       [os.path.join(dirpath, c) for c in candidates])
    for fname in candidates:
        fullpath = os.path.join(dirpath, fname)
        app.logger.info(f"[serve_fits] checking existence of {fullpath}")
        if os.path.isfile(fullpath):
            app.logger.info(f"[serve_fits] found file: {fullpath}")
            # 6a) If compressed, decompress in memory
            if fullpath.lower().endswith('.fz'):
                try:
                    with afits.open(fullpath, ignore_missing_end=True, memmap=False) as hdul:
                        app.logger.info(f"[serve_fits] opened HDUList, count={len(hdul)}")
                        bio = BytesIO()
                        hdul.writeto(bio, overwrite=True)
                        bio.seek(0)
                        app.logger.info(f"[serve_fits] decompressed to {bio.getbuffer().nbytes} bytes")
                        return Response(
                            bio.getvalue(),
                            mimetype='application/fits',
                            headers={
                                'Content-Disposition': f'inline; filename="{fname[:-3]}"'
                            }
                        )
                except Exception as e:
                    app.logger.exception(f"[serve_fits] error decompressing {fullpath}")
                    return f"Error reading FITS: {e}", 500
            # 6b) Otherwise just stream the file
            return send_file(fullpath, mimetype='application/fits')

    # 7) Not found
    app.logger.error(
        f"[serve_fits] none of the RED files exist under {dirpath}: {candidates}"
    )
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

# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5002)
