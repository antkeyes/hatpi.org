# app.py

import logging
import numpy as np
from datetime import datetime
from flask import Flask, request, render_template

from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.dialects import mysql  # For SQL logging
from models import SessionLocal, StarCatalog, Frame, Astrometry
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
        if check_coordinate_on_ccd(ra_deg, dec_deg, w_approx, margin=margin, extent=extent):
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
    2) Queries Frame & Astrometry in HPCALIB for those fields, exit_code=0,
       plus the optional date filters.
    3) Final on-CCD check using full WCS from astrometry.
    """

    fields = query_fields_by_coordinate(ra_deg, dec_deg,
                                        margin=margin, extent=extent)
    app.logger.info(f"Found {len(fields)} possible fields for RA={ra_deg}, DEC={dec_deg}.")

    if not fields:
        return []

    session = SessionLocal()
    try:
        stmt = (
            select(Frame)
            .options(joinedload(Frame.astrometry))
            .join(Frame.astrometry)
            .where(Frame.OBJECT.in_(fields))
            .where(Astrometry.exit_code == 0)
        )

        app.logger.info(f"Date filter inputs => date_type='{date_type}', date_min={date_min}, date_max={date_max}")

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

        # 💥 TEMPORARY LIMIT TO TEST QUERY EXECUTION
        stmt = stmt.limit(100)

        # 🔍 SQL DIAGNOSTIC: Show full query
        compiled_sql = stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
        app.logger.info(f"SQL Query:\n{compiled_sql}")

        frames = session.execute(stmt).scalars().all()
    finally:
        session.close()

    app.logger.info(f"Found {len(frames)} frames matching date/exit_code conditions.")

    matched = []
    for fr in frames:
        w = fr.astrometry.wcs_transform
        if w is None:
            continue

        onccd = check_coordinate_on_ccd(ra_deg, dec_deg, w, margin=0, extent=extent)
        if onccd:
            matched.append({
                'IHUID': fr.IHUID,
                'FNUM': fr.FNUM,
                'OBJECT': fr.OBJECT,
                'datetime_obs': fr.datetime_obs.isoformat() if fr.datetime_obs else None,
                'EXPTIME': fr.EXPTIME,
                'relpath': fr.relpath,
            })

    app.logger.info(f"Out of those, {len(matched)} frames are actually on the CCD.")
    return matched


##################################################
#   Flask route: /lightcurves with date filters  #
##################################################
@app.route('/data', methods=['GET', 'POST'])
def lightcurves():
    if request.method == 'POST':
        # 1) Parse RA/DEC from form
        ra_str = request.form.get('ra', '').strip()
        dec_str = request.form.get('dec', '').strip()

        # Attempt float conversion
        try:
            ra_deg = float(ra_str)
            dec_deg = float(dec_str)
        except (TypeError, ValueError):
            # Return the template with frames=[], but keep user input
            return render_template('lightcurves.html',
                                   frames=[],
                                   error="Please provide valid numeric RA/DEC in degrees.",
                                   ra=ra_str,
                                   dec=dec_str,
                                   date_type=request.form.get('date_type', 'datetime'),
                                   date_min_input=request.form.get('date_min', ''),
                                   date_max_input=request.form.get('date_max', ''))

        date_type = request.form.get('date_type', 'datetime').strip()
        date_min_input = request.form.get('date_min', '').strip()
        date_max_input = request.form.get('date_max', '').strip()

        # 2) Convert date_min/date_max as needed
        from datetime import datetime
        date_min, date_max = None, None
        if date_type == 'datetime':
            # Expecting YYYY-MM-DD
            if date_min_input:
                try:
                    date_min = datetime.strptime(date_min_input, '%Y-%m-%d')
                except ValueError:
                    pass
            if date_max_input:
                try:
                    date_max = datetime.strptime(date_max_input, '%Y-%m-%d')
                except ValueError:
                    pass
        elif date_type == 'JD':
            # Expecting float
            if date_min_input:
                try:
                    date_min = float(date_min_input)
                except ValueError:
                    pass
            if date_max_input:
                try:
                    date_max = float(date_max_input)
                except ValueError:
                    pass

        # 3) Perform the frames query
        frames_found = query_frames_by_coordinate(
            ra_deg, dec_deg,
            date_min=date_min,
            date_max=date_max,
            date_type=date_type,
        )

        if not frames_found:
            return render_template(
                'lightcurves.html',
                frames=[],
                message="No coverage found for that coordinate & date range.",
                ra=ra_str,
                dec=dec_str,
                date_type=date_type,
                date_min_input=date_min_input,
                date_max_input=date_max_input
            )

        # 4) Return the successful frames result
        return render_template(
            'lightcurves.html',
            frames=frames_found,
            ra=ra_str,  # keep original strings for display
            dec=dec_str,
            date_type=date_type,
            date_min_input=date_min_input,
            date_max_input=date_max_input
        )

    # GET => show empty form
    return render_template('lightcurves.html', frames=None)



if __name__ == '__main__':
    app.run(debug=True, port=5002)
