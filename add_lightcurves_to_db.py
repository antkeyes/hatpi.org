import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

# This script:
# - Traverses  /nfs/hatops/ar0/hatpi-landing-page/lightcurves directories
# - For each symlinked file, extracts its Gaia ID from the filename.
# - Inserts a row into the hatpi_lightcurves table with file_name, file_path, and gaia_id.

app = Flask(__name__)

# Update database URI to your actual credentials and database
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+mysqlconnector://fdw:fdw11master@localhost/file_download_website'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Define the File model, but it points to the new table hatpi_lightcurves
class LightcurveFile(db.Model):
    __tablename__ = 'hatpi_lightcurves'
    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(255), nullable=False, unique=True)
    gaia_id = db.Column(db.String(255), nullable=False)

# The directory containing the symlinks
directory = '/nfs/hatops/ar0/hatpi-landing-page/lightcurves'

#create db session and add files
with app.app_context():

    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith('.epd.tfa.fits'):
                file_path = os.path.join(root, filename)

                # Extract gaia_id from filename
                # Filename pattern: Gaia-DR2-<gaia_id>.epd.tfa.fits
                # Remove prefix and suffix:
                gaia_id = filename.replace('Gaia-DR2-', '')
                gaia_id = gaia_id.replace('.epd.tfa.fits', '')

                # Check if the file already exists in the database
                existing_file = LightcurveFile.query.filter_by(file_path=file_path).first()

                if existing_file is None: #only add file to db if it doesnt already exist
                    # Insert into the database
                    new_file = LightcurveFile(file_name=filename, file_path=file_path, gaia_id=gaia_id)
                    db.session.add(new_file)
                    print(f"Added {filename} with gaia_id={gaia_id} to the database.")

    db.session.commit()
    print("Database has been updated with new files.")
