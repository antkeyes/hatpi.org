from flask import Flask, render_template, request, send_file
from flask_sqlalchemy import SQLAlchemy
import os
import logging

# This Flask app will:
# - Connect to the file_download_website database and use the hatpi_lightcurves table.
# - Allow searching for files by partial Gaia ID.
# - Display results in lightcurves.html.
# - Provide a route for downloading files if needed.
# - No preview route for FITS files, as they are binary.
# - The route will be accessible at hatpi.org/lightcurves (via the Nginx proxy).

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

#error logging
logging.basicConfig(
    filename='/var/log/hatpi_lightcurves.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)


# Configure the app and database
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+mysqlconnector://hplc:hp11lightcurves_r@128.112.26.60/HPLC'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Define the File model corresponding to hatpi_lightcurves table
class LightcurveFile(db.Model):
    __tablename__ = 'lightcurve_files' 
    Gaia_DR2_ID = db.Column(db.BigInteger, primary_key=True)
    IHUID = db.Column(db.SmallInteger)
    OBJECT = db.Column(db.String(50))
    branch = db.Column(db.Enum('aperphot','subphot'))
    year = db.Column(db.Integer)
    path_to_file = db.Column(db.String(255), nullable=False)


@app.route('/lightcurves', methods=['GET', 'POST'])
def list_files():
    # Search for files by partial gaia_id
    search_query = request.args.get('search')
    app.logger.info(f"Received search query: {search_query}")

    results = []

    if search_query:
        # Build the wildcard pattern
        search_pattern = f"{search_query}%"
        app.logger.info(f"Searching for files matching Gaia_DR2_ID pattern: {search_pattern}")

        # Perform the database query
         # Limit the results to 100
        files = (LightcurveFile.query
                 .filter(LightcurveFile.Gaia_DR2_ID.like(search_pattern))
                 .limit(100)
                 .all())
        
        for f in files:
            filename = f"Gaia-GR2-{f.Gaia_DR2_ID}.epd.tfa.fits"
            results.append({
                'gaia_id': f.Gaia_DR2_ID,
                'filename': filename
            })


        if files:
            for f in files:
                app.logger.info(f"Found file for Gaia_DR2_ID: {f.Gaia_DR2_ID}, path: {f.path_to_file}")
        else:
            app.logger.info("No files found matching the Gaia ID.")
    else:
        app.logger.info("No search query provided, no files retrieved.")

    return render_template('lightcurves.html', files=results, search_query=search_query)

@app.route('/lightcurves/download/<int:gaia_id>')
def download_file(gaia_id):
    # Route to download the FITS file
    file_record = LightcurveFile.query.get_or_404(gaia_id)
    file_path = file_record.path_to_file

    #IMPORTANT**
    #path_to_file entries in the DB are through /home/abodi... and this is not nfs accessible
    #so path names have to be manually prefixed to be accessible via hatops server
    file_path_on_hatops = file_path.replace('/home/abodi/ar1/lctest', '/nfs/php1/ar1/P/PROJ/abodi/lctest')

    if os.path.exists(file_path_on_hatops):
        return send_file(file_path_on_hatops, as_attachment=True)
    else:
        return "File not found on server.", 404

if __name__ == '__main__':
    # Run the flask app on port 5002 as assumed in the nginx config
    app.run(host='127.0.0.1', port=5002, debug=True)
