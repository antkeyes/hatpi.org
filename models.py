# models.py
import os
from datetime import datetime

import json
import numpy as np

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    Float,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship, sessionmaker, declarative_base
from sqlalchemy import ForeignKeyConstraint


# 1) Base class for your tables
Base = declarative_base()

# 2) Minimal StarCatalog table
class StarCatalog(Base):
    __tablename__ = 'star_catalogs'

    # We'll assume the HPCALIB has these columns
    catalog_id = Column(Integer, primary_key=True)
    OBJECT     = Column(String(20))  # The field's name
    RA         = Column(Float)       # RA in degrees
    DEC        = Column(Float)       # DEC in degrees
    SIZE       = Column(Float)       # size in degrees?

    def __repr__(self):
        return f"<StarCatalog OBJ={self.OBJECT}, RA={self.RA}, DEC={self.DEC}>"

# 3) Minimal Frame table
class Frame(Base):
    __tablename__ = 'frames'
    __table_args__ = (ForeignKeyConstraint(["IHUID", "FNUM"],
                                           ["astrometry.IHUID", "astrometry.FNUM"],
                                           use_alter=True, name="fk_astrom"), )

    IHUID        = Column(Integer, primary_key=True)
    FNUM         = Column(Integer, primary_key=True)
    OBJECT       = Column(String(20))
    JD           = Column(Float)
    datetime_obs = Column(DateTime)
    EXPTIME      = Column(Float)
    date_dir     = Column(String(20))
    frame_name   = Column(String(100))
    compression  = Column(String(5))   # e.g. '.gz', '.fz', or None

    # Relationship to Astrometry (one-to-one)
    astrometry = relationship("Astrometry", back_populates="frame", uselist=False)

    def __repr__(self):
        return f"<Frame IHUID={self.IHUID}, FNUM={self.FNUM}, OBJ={self.OBJECT}>"

    @property
    def relpath(self):
        """Example of how we build relative path (as in pipeline)."""
        ihu_dir = f'ihu{self.IHUID:02d}'
        path = f"{self.date_dir}/{ihu_dir}/{self.frame_name}"
        if self.compression:
            path += self.compression
        return path

# 4) Minimal Astrometry table
class Astrometry(Base):
    __tablename__ = 'astrometry'

    IHUID     = Column(Integer, primary_key=True)
    FNUM      = Column(Integer, primary_key=True)
    exit_code = Column(Integer, nullable=False, default=0)

    CRVAL1 = Column(Float)
    CRVAL2 = Column(Float)
    CRPIX1 = Column(Float)
    CRPIX2 = Column(Float)
    CD1_1  = Column(Float)
    CD1_2  = Column(Float)
    CD2_1  = Column(Float)
    CD2_2  = Column(Float)

    A = Column(String(2000))  # SIP JSON
    B = Column(String(2000))  # SIP JSON

    frame = relationship("Frame", back_populates="astrometry", uselist=False)

    def __repr__(self):
        return (f"<Astrometry IHUID={self.IHUID}, FNUM={self.FNUM}, "
                f"exit_code={self.exit_code}>")

    @property
    def wcs_transform(self):
        """
        Rebuild the WCS using the pipeline approach. 
        We rely on a function in mywcs.py, or inline code here.
        """
        from mywcs import create_wcs  # or relative import if your structure differs

        if (self.CRVAL1 is None) or (self.exit_code != 0):
            return None

        crval = [self.CRVAL1, self.CRVAL2]
        crpix = [self.CRPIX1, self.CRPIX2]
        cdmat = np.array([[self.CD1_1, self.CD1_2],
                          [self.CD2_1, self.CD2_2]])

        # Create the base WCS
        w = create_wcs(crval, crpix, cdmat)

        # If we have A, B for SIP
        if self.A and self.B:
            try:
                import json
                from astropy.wcs import Sip
                a_arr = np.array(json.loads(self.A))
                b_arr = np.array(json.loads(self.B))
                w.sip = Sip(a_arr, b_arr, None, None, crpix)
            except:
                pass

        return w


class CalFrameQuality(Base):
    __tablename__ = 'calframe_quality'
    IHUID           = Column(Integer, primary_key=True)
    FNUM            = Column(Integer, primary_key=True)
    calframe_median = Column(Float)   # sky background in ADU

# models.py  – add after CalFrameQuality
class FrameQuality(Base):
    __tablename__ = 'frame_quality'

    IHUID    = Column(Integer, primary_key=True)
    FNUM     = Column(Integer, primary_key=True)
    MOONDIST = Column(Float)   # Moon distance in degrees
    SUNELEV  = Column(Float)   # Sun elevation in degrees


# ----------------------------------------------------------------
# 5) Create an engine & Session that the rest of the app can use
# ----------------------------------------------------------------

# Typically you get credentials from environment variables:
DB_HOST = os.environ.get("HPCALIB_DB_HOST")
DB_USER = os.environ.get("HPCALIB_DB_USER")
DB_PASS = os.environ.get("HPCALIB_DB_PASS")
DB_NAME = os.environ.get("HPCALIB_DB_NAME")

DB_PORT = os.environ.get("HPCALIB_DB_PORT", "3306")

DB_DRIVER = "mysql+pymysql"

DATABASE_URL = f"{DB_DRIVER}://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# Note: We do NOT call Base.metadata.create_all(engine) because HPCALIB already exists.
# We'll just read from it. If you needed to create the tables, you’d do that here.
