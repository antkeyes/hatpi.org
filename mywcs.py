# mywcs.py
import numpy as np
from astropy.wcs import WCS, Sip

def create_wcs(crval, crpix, cdmat, sip_pars=None):
    """
    Create a WCS with the given CRVAL, CRPIX, CD matrix,
    plus optional SIP terms.
    """
    w = WCS(naxis=2)
    w.wcs.crval = crval
    w.wcs.crpix = crpix
    w.wcs.cd = cdmat
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]

    if sip_pars is not None:
        a_arr, b_arr = sip_pars
        if a_arr is not None and b_arr is not None:
            w.sip = Sip(a_arr, b_arr, None, None, crpix)

    return w

def create_simple_wcs(crval, crpix=(1024, 1024), pixsize=19.62):
    """
    The simplified WCS used for star catalogs to check if RA/DEC might be on CCD.
    """
    w = WCS(naxis=2)
    deg_per_pix = pixsize / 3600.0  # arcsec -> deg
    w.wcs.crval = crval
    w.wcs.crpix = crpix
    w.wcs.cdelt = [-deg_per_pix, deg_per_pix]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    return w
