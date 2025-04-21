# app.py

import logging
import numpy as np
import math
from datetime import datetime
from flask import Flask, request, render_template, jsonify

from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.dialects import mysql  # For SQL logging
from models import SessionLocal, StarCatalog, Frame, Astrometry, CalFrameQuality
from mywcs import create_simple_wcs
from astropy.wcs import NoConvergence

app = Flask(__name__)

# Set the logging level and format.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

##################################
#   Simple On-CCD Check Utility  #
##################################
def check_coordinate_on_ccd(ra_deg, dec_deg, wcs, margin=0,
                            extent=(0, 2048, 0, 2048)):
    """Check if (ra_deg, dec_deg) is on the CCD using a WCS projection."""
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

######################################
#   Stage 1: Find candidate fields   #
######################################
def query_fields_by_coordinate(ra_deg, dec_deg, margin=100,
                               extent=(0, 2048, 0, 2048),
                               crpix=(1024, 1024), pixsize=19.62):
    """
    Returns a list of field OBJECT names from StarCatalog that
    might contain (ra_deg, dec_deg).
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

    fields_inccd = []
    for (obj_name, cat_ra, cat_dec) in rows:
        # cat_ra, cat_dec are in degrees already (pipeline style).
        w_approx = create_simple_wcs((cat_ra, cat_dec), crpix=crpix, pixsize=pixsize)
        # Quick coverage check
        if check_coordinate_on_ccd(ra_deg, dec_deg, w_approx, margin=margin):
            fields_inccd.append(obj_name)

    return fields_inccd


############################################
#   Stage 2: Query frames in those fields  #
############################################
def query_frames_by_coordinate(ra_deg, dec_deg,
                               date_min=None,
                               date_max=None,
                               date_type='datetime',
                               margin=100,
                               extent=(0, 2048, 0, 2048)):
    """
    1) Finds candidate fields via query_fields_by_coordinate.
    2) Queries Frame, Astrometry, and CalFrameQuality (for sky background) in HPCALIB
       for those fields, exit_code=0, plus the optional date filters.
    3) Final on‑CCD check using full WCS from astrometry.
    """

    # 1) Find candidate fields
    fields = query_fields_by_coordinate(ra_deg, dec_deg,
                                        margin=margin, extent=extent)
    app.logger.info(f"Candidate fields: {fields}")
    app.logger.info(f"Found {len(fields)} possible fields for RA={ra_deg}, DEC={dec_deg}.")

    if not fields:
        return []

    session = SessionLocal()
    try:
        # 2) Build query: join Frame → Astrometry, then outer‑join CalFrameQuality
        stmt = (
            select(
                Frame,
                CalFrameQuality.calframe_median.label('sky_bg')
            )
            .options(joinedload(Frame.astrometry))
            .join(Frame.astrometry)
            .outerjoin(
                CalFrameQuality,
                (Frame.IHUID == CalFrameQuality.IHUID) &
                (Frame.FNUM  == CalFrameQuality.FNUM)
            )
            .where(Frame.OBJECT.in_(fields))
            .where(Astrometry.exit_code == 0)
        )

        app.logger.info(
            f"Date filter inputs => date_type='{date_type}', "
            f"date_min={date_min}, date_max={date_max}"
        )

        # 2a) Apply date filters
        if date_type == 'datetime':
            if date_min is not None:
                stmt = stmt.where(Frame.datetime_obs >= date_min)
                app.logger.info(f"Applying Frame.datetime_obs >= {date_min}")
            if date_max is not None:
                stmt = stmt.where(Frame.datetime_obs <= date_max)
                app.logger.info(f"Applying Frame.datetime_obs <= {date_max}")
        elif date_type == 'JD':
            if date_min is not None:
                jdmin = date_min - 2400000
                stmt = stmt.where(Frame.JD >= jdmin)
                app.logger.info(f"Applying Frame.JD >= {jdmin} (from {date_min})")
            if date_max is not None:
                jdmax = date_max - 2400000
                stmt = stmt.where(Frame.JD <= jdmax)
                app.logger.info(f"Applying Frame.JD <= {jdmax} (from {date_max})")
        else:
            app.logger.warning(f"Unknown date_type: {date_type}")

        # 2b) Limit for testing / pagination
        # stmt = stmt.limit(100)

        # 2c) Log the generated SQL
        compiled_sql = stmt.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True}
        )
        app.logger.info(f"SQL Query:\n{compiled_sql}")

        # 2d) Execute and fetch rows: each row is (Frame, sky_bg)
        rows = session.execute(stmt).all()

    finally:
        session.close()

    app.logger.info(f"Found {len(rows)} candidate rows before on‑CCD filtering.")

    # 3) Final on‑CCD check and build result list
    matched = []
    for fr, sky_bg in rows:
        w = fr.astrometry.wcs_transform
        if w is None:
            continue

        onccd = check_coordinate_on_ccd(
            ra_deg, dec_deg, w,
            margin=0
        )
        if onccd:
            matched.append({
                'IHUID':        fr.IHUID,
                'FNUM':         fr.FNUM,
                'OBJECT':       fr.OBJECT,
                'datetime_obs': fr.datetime_obs.isoformat()
                                if fr.datetime_obs else None,
                'EXPTIME':      fr.EXPTIME,
                'relpath':      fr.relpath,
                'sky_bg':       sky_bg,
            })

    app.logger.info(f"Out of those, {len(matched)} frames are actually on the CCD.")
    return matched



##################################################
#   Flask route: /lightcurves with date filters  #
##################################################

@app.route('/data', methods=['GET', 'POST'])
def lightcurves():
    if request.method == 'POST':
        # 1) Read RA/DEC strings from the form
        ra_string = request.form.get('ra', '').strip()
        dec_string = request.form.get('dec', '').strip()

        # 2) Convert RA/DEC to floats
        try:
            ra_degrees = float(ra_string)
            dec_degrees = float(dec_string)
        except (TypeError, ValueError):
            # Invalid input: re‑render with an error message
            return render_template(
                'lightcurves.html',
                frames=[],
                error="Please provide valid numeric Right Ascension and Declination in degrees.",
                ra=ra_string,
                dec=dec_string,
                date_type=request.form.get('date_type', 'datetime'),
                date_min_input=request.form.get('date_min', ''),
                date_max_input=request.form.get('date_max', '')
            )

        # 3) Read date filters from the form
        date_type_string      = request.form.get('date_type', 'datetime').strip()
        date_minimum_input    = request.form.get('date_min', '').strip()
        date_maximum_input    = request.form.get('date_max', '').strip()

        # 4) Parse date_minimum and date_maximum into appropriate types
        date_minimum = None
        date_maximum = None
        if date_type_string == 'datetime':
            if date_minimum_input:
                try:
                    date_minimum = datetime.strptime(date_minimum_input, '%Y-%m-%d')
                except ValueError:
                    pass
            if date_maximum_input:
                try:
                    date_maximum = datetime.strptime(date_maximum_input, '%Y-%m-%d')
                except ValueError:
                    pass
        elif date_type_string == 'JD':
            if date_minimum_input:
                try:
                    date_minimum = float(date_minimum_input)
                except ValueError:
                    pass
            if date_maximum_input:
                try:
                    date_maximum = float(date_maximum_input)
                except ValueError:
                    pass

        # 5) Determine requested page number (default to 1) and page size
        page_number = 1
        try:
            page_number = int(request.form.get('page', '1'))
        except ValueError:
            page_number = 1
        page_size = 50

        # 6) Run the full query (no SQL LIMIT)
        all_frames = query_frames_by_coordinate(
            ra_degrees,
            dec_degrees,
            date_min=date_minimum,
            date_max=date_maximum,
            date_type=date_type_string,
        )

        # 7) Compute total count and total pages
        total_frame_count = len(all_frames)
        total_pages = max(1, math.ceil(total_frame_count / page_size))

        # 8) Clamp page_number to valid range
        if page_number < 1:
            page_number = 1
        elif page_number > total_pages:
            page_number = total_pages

        # 9) Slice out only the frames for the current page
        start_index = (page_number - 1) * page_size
        end_index = start_index + page_size
        frames_for_display = all_frames[start_index:end_index]

        # 10) If no frames at all, show a “no coverage” message
        if total_frame_count == 0:
            return render_template(
                'lightcurves.html',
                frames=[],
                total_count=0,
                message="No coverage found for that coordinate and date range.",
                ra=ra_string,
                dec=dec_string,
                date_type=date_type_string,
                date_min_input=date_minimum_input,
                date_max_input=date_maximum_input,
                page=1,
                total_pages=1
            )

        # 11) Render the template with both the true total and the current page’s frames
        return render_template(
            'lightcurves.html',
            frames=frames_for_display,
            total_count=total_frame_count,
            page=page_number,
            total_pages=total_pages,
            ra=ra_string,
            dec=dec_string,
            date_type=date_type_string,
            date_min_input=date_minimum_input,
            date_max_input=date_maximum_input
        )

    # If it’s a GET request, just render the empty search form
    return render_template('lightcurves.html', frames=None)



from flask import Response, jsonify, request
from datetime import datetime
from io import StringIO, BytesIO
import csv
from astropy.table import Table

@app.route('/api/data', methods=['POST'])
def data_api():
    """
    POST /api/data
    Accepts JSON payload with ra, dec, date_type, date_min, date_max.
    Optional query-string parameter ?format=csv or ?format=votable selects output format.
    """
    # 0) Determine requested output format
    fmt = request.args.get('format', 'json').lower()

    # 1) Validate that the body is JSON
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    data = request.get_json()

    # 2) Parse and validate RA/DEC
    try:
        ra_deg  = float(data.get('ra', '').strip())
        dec_deg = float(data.get('dec', '').strip())
    except Exception:
        return jsonify({"error": "Invalid RA or DEC. They must be numeric."}), 400

    # 3) Parse and validate date filters
    date_type      = data.get('date_type', 'datetime').strip()
    date_min_input = data.get('date_min', '').strip()
    date_max_input = data.get('date_max', '').strip()
    date_min = date_max = None

    if date_type == 'datetime':
        if date_min_input:
            try:
                date_min = datetime.strptime(date_min_input, '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": "Invalid date_min format. Use YYYY-MM-DD."}), 400
        if date_max_input:
            try:
                date_max = datetime.strptime(date_max_input, '%Y-%m-%d')
            except ValueError:
                return jsonify({"error": "Invalid date_max format. Use YYYY-MM-DD."}), 400

    elif date_type == 'JD':
        try:
            if date_min_input:
                date_min = float(date_min_input)
            if date_max_input:
                date_max = float(date_max_input)
        except ValueError:
            return jsonify({"error": "Invalid JD date_min/date_max. Must be numeric."}), 400

    else:
        return jsonify({"error": "Unsupported date_type"}), 400

    # 4) Query the database
    frames_found = query_frames_by_coordinate(
        ra_deg, dec_deg,
        date_min=date_min,
        date_max=date_max,
        date_type=date_type
    )

    # 5) Normalize and enrich results
    results = []
    for f in frames_found:
        dt = f.get('datetime_obs')
        if dt and not dt.endswith('Z'):
            dt = dt + 'Z'
        results.append({
            "object":             f.get("OBJECT", "").lower(),
            "ihuid":              f.get("IHUID"),
            "fnum":               f.get("FNUM"),
            "datetime_obs":       dt,
            "exptime":            f.get("EXPTIME"),
            "sky_background_adu": f.get("sky_bg"),
            "download_url":       f"https://hatpi.org/data/{f.get('relpath')}"
        })

    # 6) Branch on requested format
    if fmt == 'csv':
        # Build CSV in memory
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "object", "ihuid", "fnum", "datetime_obs",
            "exptime", "sky_background_adu", "download_url"
        ])
        for row in results:
            writer.writerow([
                row["object"], row["ihuid"], row["fnum"],
                row["datetime_obs"], row["exptime"],
                row["sky_background_adu"], row["download_url"]
            ])
        return Response(output.getvalue(), mimetype='text/csv')

    elif fmt == 'votable':
        # Build VOTable using Astropy
        table = Table(rows=results, names=list(results[0].keys()) if results else [])
        buffer = BytesIO()
        table.write(buffer, format='votable')
        return Response(buffer.getvalue(), mimetype='application/x-votable+xml')

    else:
        # Default: JSON envelope
        return jsonify({
            "total_frames": len(results),
            "frames":       results
        }), 200





if __name__ == '__main__':
    app.run(debug=True, port=5002)
