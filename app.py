from flask import Flask, render_template, request, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import cast
from sqlalchemy.types import String
import os
import logging
from astropy.io import fits
import plotly.graph_objs as go
from plotly.offline import plot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

#error logging
logging.basicConfig(
    filename='/var/log/hatpi_lightcurves.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)

app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+mysqlconnector://hplc:hp11lightcurves_r@128.112.26.60/HPLC'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

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
    search_query = request.args.get('search')

    app.logger.info(f"Received search query: {search_query}")

    results = []

    if search_query:
        search_pattern = f"{search_query}%"
        app.logger.info(f"Searching for files matching Gaia_DR2_ID: {search_pattern}")

        query = LightcurveFile.query.filter(cast(LightcurveFile.Gaia_DR2_ID, String).like(search_pattern))

        files = query.limit(100).all()

        for f in files:
            filename = f"Gaia-DR2-{f.Gaia_DR2_ID}.epd.tfa.fits"
            results.append({
                'gaia_id': f.Gaia_DR2_ID,
                'filename': filename
            })

        if files:
            for f in files:
                app.logger.info(f"Found file Gaia_DR2_ID: {f.Gaia_DR2_ID}, path: {f.path_to_file}")
        else:
            app.logger.info("No files found matching the query.")
    else:
        app.logger.info("No search query provided.")

    return render_template('lightcurves.html', files=results, search_query=search_query)




@app.route('/lightcurves/download/<int:gaia_id>')
def download_file(gaia_id):
    file_record = LightcurveFile.query.get_or_404(gaia_id)
    file_path = file_record.path_to_file

    # Adjust for NFS path only if it starts with the expected prefix
    if file_path.startswith('/P'):
        file_path_on_hatops = file_path.replace('/P', '/nfs/php2/ar0/P', 1)  # Replace only the first occurrence
    else:
        file_path_on_hatops = file_path

    app.logger.info(f"sending file from: {file_path_on_hatops}")
    

    if os.path.exists(file_path_on_hatops):
        return send_file(file_path_on_hatops, as_attachment=True)
    else:
        return "File not found on server.", 404
    


@app.route('/lightcurves/plot/<string:gaia_id>', methods=['GET'])
def plot_lightcurve(gaia_id):
    app.logger.info(f"Received request to plot light curve for Gaia_DR2_ID: {gaia_id}")
    
    # Validate and convert `gaia_id` to integer
    try:
        gaia_id_int = int(gaia_id)
    except ValueError:
        app.logger.error(f"Invalid Gaia_DR2_ID: {gaia_id}")
        return jsonify({'error': 'Invalid Gaia_DR2_ID.'}), 400
    
    # Retrieve the file record from the database using Session.get()
    file_record = db.session.get(LightcurveFile, gaia_id_int)
    if not file_record:
        app.logger.error(f"No file found for Gaia_DR2_ID: {gaia_id}")
        return jsonify({'error': 'File not found in database.'}), 404

    # Adjust the file path for NFS
    file_path = file_record.path_to_file
    file_path_on_hatops = file_path.replace('/P', '/nfs/php2/ar0/P', 1)
    app.logger.info(f"Adjusted file path for Gaia_DR2_ID {gaia_id}: {file_path_on_hatops}")

    if not os.path.exists(file_path_on_hatops):
        app.logger.error(f"File not found on server: {file_path_on_hatops}")
        return jsonify({'error': 'File not found on server.'}), 404

    try:
        # Open the FITS file and extract data
        with fits.open(file_path_on_hatops) as hdul:
            hdul.info()
            data = hdul[1].data

            # Extract relevant columns
            time = data['TIME']           # Replace 't' with 'TIME'
            mag = data['FITMAG0']         # Replace 'mag0' with 'FITMAG0'
            err = data['ERR0']            # Replace 'err0' with 'ERR0'

            # Optional: Shift time for better readability
            time_shifted = time - time.min()

        # Create an interactive scatter plot with error bars
        trace = go.Scatter(
            x=time_shifted,
            y=mag,
            mode='markers',
            name='FITMAG0',
            error_y=dict(
                type='data',
                array=err,
                visible=True
            ),
            marker=dict(
                size=5,
                color='blue',
                opacity=0.7
            )
        )

        # Define the layout of the plot
        layout = go.Layout(
            title=f'File Preview for Gaia_DR2_ID {gaia_id}',
            xaxis=dict(
                title=f"Time (days since {time.min():.3f})",
                showgrid=True,
                zeroline=False
            ),
            yaxis=dict(
                title='Magnitude',
                autorange='reversed',  # Invert y-axis for magnitudes
                showgrid=True,
                zeroline=False
            ),
            hovermode='closest'
        )

        # Combine trace and layout into a figure
        fig = go.Figure(data=[trace], layout=layout)

        # Convert the figure to JSON
        graphJSON = fig.to_json()

        app.logger.info(f"Successfully generated plot for Gaia_DR2_ID: {gaia_id}")

        return graphJSON

    except Exception as e:
        app.logger.exception(f"Error generating plot for Gaia_DR2_ID {gaia_id}: {e}")
        return jsonify({'error': 'Failed to generate plot.'}), 500





if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5002, debug=True)
