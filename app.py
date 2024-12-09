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

# Configure the app and database
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+mysqlconnector://fdw:fdw11master@localhost/file_download_website'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Define the File model corresponding to hatpi_lightcurves table
class LightcurveFile(db.Model):
    __tablename__ = 'hatpi_lightcurves'
    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(255), nullable=False, unique=True)
    gaia_id = db.Column(db.String(255), nullable=False)

@app.route('/lightcurves', methods=['GET', 'POST'])
def list_files():
    # Search for files by partial gaia_id
    search_query = request.args.get('search')
    logging.info(f"Received search query: {search_query}")

    if search_query:
        # Build the wildcard pattern
        search_pattern = f"%{search_query}%"
        logging.info(f"Searching for files matching gaia_id pattern: {search_pattern}")

        # Perform the database query
        files = LightcurveFile.query.filter(LightcurveFile.gaia_id.like(search_pattern)).all()

        if files:
            for f in files:
                logging.info(f"Found file: {f.file_name} gaia_id: {f.gaia_id} path: {f.file_path}")
        else:
            logging.info("No files found matching the Gaia ID.")
    else:
        files = []
        logging.info("No search query provided, no files retrieved.")

    return render_template('lightcurves.html', files=files, search_query=search_query)

@app.route('/lightcurves/download/<int:file_id>')
def download_file(file_id):
    # Route to download the FITS file
    file = LightcurveFile.query.get_or_404(file_id)
    file_path = file.file_path

    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        return "File not found.", 404

if __name__ == '__main__':
    # Run the flask app on port 5001 as assumed in the nginx config
    app.run(host='127.0.0.1', port=5002, debug=True)
