from __future__ import print_function
from builtins import zip
from builtins import object
import numpy as np
import healpy as hp
import pandas as pd
from scipy.spatial import cKDTree as kdtree
from lsst.sims.utils import _hpid2RaDec, calcLmstLast
from astropy.coordinates import SkyCoord
from astropy import units as u
import ephem
import os
import sys
from lsst.utils import getPackageDir
import sqlite3 as db


def set_default_nside(nside=None):
    """
    Utility function to set a default nside value across the scheduler.

    Parameters
    ----------
    nside : int (None)
        A valid healpixel nside.
    """
    if not hasattr(set_default_nside, 'nside'):
        if nside is None:
            nside = 64
        set_default_nside.side = nside
    return set_default_nside.side


def empty_observation():
    """
    Return a numpy array that could be a handy observation record

    XXX:  Should this really be "empty visit"? Should we have "visits" made
    up of multple "observations" to support multi-exposure time visits?

    XXX-Could add a bool flag for "observed". Then easy to track all proposed
    observations. Could also add an mjd_min, mjd_max for when an observation should be observed.
    That way we could drop things into the queue for DD fields.

    Returns
    -------
    numpy array

    Notes
    -----
    The numpy fields have the following structure
    RA : float
       The Right Acension of the observation (center of the field) (Radians)
    dec : float
       Declination of the observation (Radians)
    mjd : float
       Modified Julian Date at the start of the observation (time shutter opens)
    exptime : float
       Total exposure time of the visit (seconds)
    filter : str
        The filter used. Should be one of u, g, r, i, z, y.
    rotSkyPos : float
        The rotation angle of the camera relative to the sky E of N (Radians)
    nexp : int
        Number of exposures in the visit.
    airmass : float
        Airmass at the center of the field
    FWHMeff : float
        The effective seeing FWHM at the center of the field. (arcsec)
    skybrightness : float
        The surface brightness of the sky background at the center of the
        field. (mag/sq arcsec)
    night : int
        The night number of the observation (days)
    """
    names = ['RA', 'dec', 'mjd', 'exptime', 'filter', 'rotSkyPos', 'nexp',
             'airmass', 'FWHMeff', 'skybrightness', 'night', 'slewtime']
    # units of rad, rad,   days,  seconds,   string, radians (E of N?)
    types = [float, float, float, float, '|1S', float, int, float, float, float, int, float]
    result = np.zeros(1, dtype=list(zip(names, types)))
    return result


def empty_scheduled_observation():
    """
    Same as empty observation, but with mjd_min, mjd_max columns
    """
    start = empty_observation()
    names = start.dtype.names
    types = start.dtype.types
    names.extend(['mjd_min', 'mjd_max'])
    types.extend([float, float])

    result = np.zeros(1, dtype=list(zip(names, types)))
    return result


def read_fields():
    """
    Read in the old Field coordinates
    Returns
    -------
    numpy.array
        With RA and dec in radians.
    """
    names = ['id', 'RA', 'dec']
    types = [int, float, float]
    data_dir = os.path.join(getPackageDir('sims_featureScheduler'), 'python/lsst/sims/featureScheduler/')
    filepath = os.path.join(data_dir, 'fieldID.lis')
    fields = np.loadtxt(filepath, dtype=list(zip(names, types)))
    fields['RA'] = np.radians(fields['RA'])
    fields['dec'] = np.radians(fields['dec'])
    return fields


def treexyz(ra, dec):
    """
    Utility to convert RA,dec postions in x,y,z space, useful for constructing KD-trees.
    
    Parameters
    ----------
    ra : float or array
        RA in radians
    dec : float or array
        Dec in radians

    Returns
    -------
    x,y,z : floats or arrays
        The position of the given points on the unit sphere.
    """
    # Note ra/dec can be arrays.
    x = np.cos(dec) * np.cos(ra)
    y = np.cos(dec) * np.sin(ra)
    z = np.sin(dec)
    return x, y, z


def hp_kd_tree(nside=set_default_nside(), leafsize=100):
    """
    Generate a KD-tree of healpixel locations

    Parameters
    ----------
    nside : int
        A valid healpix nside
    leafsize : int (100)
        Leafsize of the kdtree

    Returns
    -------
    tree : scipy kdtree
    """
    hpid = np.arange(hp.nside2npix(nside))
    ra, dec = _hpid2RaDec(nside, hpid)
    x, y, z = treexyz(ra, dec)
    tree = kdtree(list(zip(x, y, z)), leafsize=leafsize, balanced_tree=False, compact_nodes=False)
    return tree


def rad_length(radius=1.75):
    """
    Convert an angular radius into a physical radius for a kdtree search.

    Parameters
    ----------
    radius : float
        Radius in degrees.
    """
    x0, y0, z0 = (1, 0, 0)
    x1, y1, z1 = treexyz(np.radians(radius), 0)
    result = np.sqrt((x1-x0)**2+(y1-y0)**2+(z1-z0)**2)
    return result


class hp_in_lsst_fov(object):
    """
    Return the healpixels within a pointing. A very simple LSST camera model with
    no chip/raft gaps.
    """
    def __init__(self, nside=set_default_nside(), fov_radius=1.75):
        """
        Parameters
        ----------
        fov_radius : float (1.75)
            Radius of the filed of view in degrees
        """
        self.tree = hp_kd_tree(nside=nside)
        self.radius = rad_length(fov_radius)

    def __call__(self, ra, dec):
        """
        Parameters
        ----------
        ra : float
            RA in radians
        dec : float
            Dec in radians

        Returns
        -------
        indx : numpy array
            The healpixels that are within the FoV
        """
        x, y, z = treexyz(np.max(ra), np.max(dec))
        indices = self.tree.query_ball_point((x, y, z), self.radius)
        return np.array(indices)


def ra_dec_hp_map(nside=set_default_nside()):
    """
    Return all the RA,dec points for the centers of a healpix map
    """
    ra, dec = _hpid2RaDec(nside, np.arange(hp.nside2npix(nside)))
    return ra, dec


def WFD_healpixels(nside=set_default_nside(), dec_min=-60., dec_max=0.):
    """
    Define a wide fast deep region. Return a healpix map with WFD pixels as 1.
    """
    ra, dec = ra_dec_hp_map(nside=nside)
    result = np.zeros(ra.size)
    good = np.where((dec >= np.radians(dec_min)) & (dec <= np.radians(dec_max)))
    result[good] += 1
    return result


def SCP_healpixels(nside=set_default_nside(), dec_max=-60.):
    """
    Define the South Celestial Pole region. Return a healpix map with SCP pixels as 1.
    """
    ra, dec = ra_dec_hp_map(nside=nside)
    result = np.zeros(ra.size)
    good = np.where(dec < np.radians(dec_max))
    result[good] += 1
    return result


def NES_healpixels(nside=set_default_nside(), width=15, dec_min=0., fill_gap=True):
    """
    Define the North Ecliptic Spur region. Return a healpix map with NES pixels as 1.
    """
    ra, dec = ra_dec_hp_map(nside=nside)
    result = np.zeros(ra.size)
    coord = SkyCoord(ra=ra*u.rad, dec=dec*u.rad)
    eclip_lat = coord.barycentrictrueecliptic.lat.radian
    good = np.where((np.abs(eclip_lat) <= np.radians(width)) & (dec > dec_min))
    result[good] += 1

    if fill_gap:
        good = np.where((dec > np.radians(dec_min)) & (ra < np.radians(180)) &
                        (dec < np.radians(width)))
        result[good] = 1

    return result


def galactic_plane_healpixels(nside=set_default_nside(), center_width=10., end_width=4.,
                              gal_long1=70., gal_long2=290.):
    """
    Define the Galactic Plane region. Return a healpix map with GP pixels as 1.
    """
    ra, dec = ra_dec_hp_map(nside=nside)
    result = np.zeros(ra.size)
    coord = SkyCoord(ra=ra*u.rad, dec=dec*u.rad)
    g_long, g_lat = coord.galactic.l.radian, coord.galactic.b.radian
    good = np.where((g_long < np.radians(gal_long1)) & (np.abs(g_lat) < np.radians(center_width)))
    result[good] += 1
    good = np.where((g_long > np.radians(gal_long2)) & (np.abs(g_lat) < np.radians(center_width)))
    result[good] += 1
    # Add tapers
    slope = -(np.radians(center_width)-np.radians(end_width))/(np.radians(gal_long1))
    lat_limit = slope*g_long+np.radians(center_width)
    outside = np.where((g_long < np.radians(gal_long1)) & (np.abs(g_lat) > np.abs(lat_limit)))
    result[outside] = 0
    slope = (np.radians(center_width)-np.radians(end_width))/(np.radians(360. - gal_long2))
    b = np.radians(center_width)-np.radians(360.)*slope
    lat_limit = slope*g_long+b
    outside = np.where((g_long > np.radians(gal_long2)) & (np.abs(g_lat) > np.abs(lat_limit)))
    result[outside] = 0

    return result


def generate_goal_map(nside=set_default_nside(), NES_fraction = .3, WFD_fraction = 1., SCP_fraction=0.4,
                      GP_fraction = 0.2,
                      NES_width=15., NES_dec_min=0., NES_fill=True,
                      SCP_dec_max=-60., gp_center_width=10.,
                      gp_end_width=4., gp_long1=70., gp_long2=290.,
                      wfd_dec_min=-60., wfd_dec_max=0.):
    """
    Handy function that will put together a target map in the proper order.
    """

    # Note, some regions overlap, thus order regions are added is important.
    result = np.zeros(hp.nside2npix(nside), dtype=float)
    result += NES_fraction*NES_healpixels(nside=nside, width=NES_width,
                                          dec_min=NES_dec_min, fill_gap=NES_fill)
    wfd = WFD_healpixels(nside=nside, dec_min=wfd_dec_min, dec_max=wfd_dec_max)
    result[np.where(wfd != 0)] = 0
    result += WFD_fraction*wfd
    scp = SCP_healpixels(nside=nside, dec_max=SCP_dec_max)
    result[np.where(scp != 0)] = 0
    result += SCP_fraction*scp
    gp = galactic_plane_healpixels(nside=nside, center_width=gp_center_width,
                                   end_width=gp_end_width, gal_long1=gp_long1,
                                   gal_long2=gp_long2)
    result[np.where(gp != 0)] = 0
    result += GP_fraction*gp
    return result


def standard_goals(nside=set_default_nside()):
    """
    A quick fucntion to generate the "standard" goal maps.
    """
    result = {}
    result['u'] = generate_goal_map(nside=nside, NES_fraction=0.,
                                    WFD_fraction=0.31, SCP_fraction=0.15,
                                    GP_fraction=0.15)
    result['g'] = generate_goal_map(nside=nside, NES_fraction=0.2,
                                    WFD_fraction=0.44, SCP_fraction=0.15,
                                    GP_fraction=0.15)
    result['r'] = generate_goal_map(nside=nside, NES_fraction=0.46,
                                    WFD_fraction=1.0, SCP_fraction=0.15,
                                    GP_fraction=0.15)
    result['i'] = generate_goal_map(nside=nside, NES_fraction=0.46,
                                    WFD_fraction=1.0, SCP_fraction=0.15,
                                    GP_fraction=0.15)
    result['z'] = generate_goal_map(nside=nside, NES_fraction=0.4,
                                    WFD_fraction=0.9, SCP_fraction=0.15,
                                    GP_fraction=0.15)
    result['y'] = generate_goal_map(nside=nside, NES_fraction=0.,
                                    WFD_fraction=0.9, SCP_fraction=0.15,
                                    GP_fraction=0.15)

    return result


def sim_runner(observatory, scheduler, mjd_start=None, survey_length=3., filename=None):
    """
    run a simulation
    """

    if mjd_start is None:
        mjd = observatory.mjd
        mjd_start = mjd + 0
    else:
        observatory.mjd = mjd
        observatory.ra = None
        observatory.dec = None
        observatory.status = None
        observatory.filtername = None

    end_mjd = mjd + survey_length
    scheduler.update_conditions(observatory.return_status())
    observations = []
    mjd_track = mjd + 0
    step = 1./24.
    mjd_run = end_mjd-mjd_start

    while mjd < end_mjd:
        desired_obs = scheduler.request_observation()
        attempted_obs = observatory.attempt_observe(desired_obs)
        if attempted_obs is not None:
            scheduler.add_observation(attempted_obs)
            observations.append(attempted_obs)
        else:
            scheduler.flush_queue()
        scheduler.update_conditions(observatory.return_status())
        mjd = observatory.mjd
        if (mjd-mjd_track) > step:
            progress = float(mjd-mjd_start)/mjd_run*100
            text = "\rprogress = %.1f%%" % progress
            sys.stdout.write(text)
            sys.stdout.flush()
            mjd_track = mjd+0

    print('Completed %i observations' % len(observations))
    observations = np.array(observations)[:, 0]
    if filename is not None:
        observations2sqlite(observations, filename=filename)
    return observatory, scheduler, observations


def observations2sqlite(observations, filename='observations.db'):
    """
    Take an array of observations and dump it to a sqlite3 database

    Parameters
    ----------
    observations : numpy.array
        An array of executed observations
    filename : str (observations.db)
        Filename to save sqlite3 to. Value of None will skip
        writing out file.

    Returns
    -------
    observations : numpy.array
        The observations array updated to have angles in degrees and
        any added columns
    """

    # XXX--Here is a good place to add any missing columns, e.g., alt,az

    # Convert to degrees for output
    observations['RA'] = np.degrees(observations['RA'])
    observations['dec'] = np.degrees(observations['dec'])
    observations['rotSkyPos'] = np.degrees(observations['rotSkyPos'])

    if filename is not None:
        df = pd.DataFrame(observations)
        con = db.connect(filename)
        df.to_sql('SummaryAllProps', con, index_label='observationId')
    return observations


def sqlite2observations(filename='observations.db'):
    """
    Restore a databse of observations.
    """
    con = db.connect(filename)
    df = pd.read_sql('select * from SummaryAllProps;', con)
    return df


def inrange(inval, minimum=-1., maximum=1.):
    """
    Make sure values are within min/max
    """
    inval = np.array(inval)
    below = np.where(inval < minimum)
    inval[below] = minimum
    above = np.where(inval > maximum)
    inval[above] = maximum
    return inval


def stupidFast_RaDec2AltAz(ra, dec, lat, lon, mjd, lmst=None):
    """
    Convert Ra,Dec to Altitude and Azimuth.

    Coordinate transformation is killing performance. Just use simple equations to speed it up
    and ignore abberation, precesion, nutation, nutrition, etc.

    Parameters
    ----------
    ra : array_like
        RA, in radians.
    dec : array_like
        Dec, in radians. Must be same length as `ra`.
    lat : float
        Latitude of the observatory in radians.
    lon : float
        Longitude of the observatory in radians.
    mjd : float
        Modified Julian Date.

    Returns
    -------
    alt : numpy.array
        Altitude, same length as `ra` and `dec`. Radians.
    az : numpy.array
        Azimuth, same length as `ra` and `dec`. Radians.
    """
    if lmst is None:
        lmst, last = calcLmstLast(mjd, lon)
        lmst = lmst/12.*np.pi  # convert to rad
    ha = lmst-ra
    sindec = np.sin(dec)
    sinlat = np.sin(lat)
    coslat = np.cos(lat)
    sinalt = sindec*sinlat+np.cos(dec)*coslat*np.cos(ha)
    sinalt = inrange(sinalt)
    alt = np.arcsin(sinalt)
    cosaz = (sindec-np.sin(alt)*sinlat)/(np.cos(alt)*coslat)
    cosaz = inrange(cosaz)
    az = np.arccos(cosaz)
    signflip = np.where(np.sin(ha) > 0)
    az[signflip] = 2.*np.pi-az[signflip]
    return alt, az


def sort_pointings(observations, order_first='azimuth'):
    """
    Try to sort a group of pointings to be executed in a good order

    Parameters
    ----------
    observations : list of observation objects
        The observations we want to sort.
    order : str (azimuth)
        Sort by azimuth or altitude first.

    Returns
    -------
    The observations sorted in a good order
    """

    obs_array = np.array(observations)[:, 0]
    # compute alt-az and raster in the correct way.
    # Maybe take some windows. 
    # Note that the az rotation is a problem near zenith. 
    # does a greedy walk do a good job?
    return

def max_altitude(dec, lsst_lat):
    """
    evaluates the maximum altitude that fields can ever achieve.

    Parameters
    ----------
    dec : numpy.array
        declination of the fields
    lsst_lat  : float
        Lattitude of the LSST site

    Returns
    -------
    max_alt : numpy.array
        Maximum altitudes. Radians.
    """
    max_alt = lsst_lat + np.pi/2. - dec
    max_alt = np.where(dec >= lsst_lat, lsst_lat + np.pi/2. - dec, -lsst_lat + np.pi/2. + dec)
    return max_alt


def alt_allocation(alt, dec, lsst_lat, filter_name='r'):
    """
    Allocates altitude to each filter, so there is a best normalized altitude for each filter

    Parameters
    ----------
    alt : numpy.array
        altitude of the fields
    dec : numpy.array
        declination of the fields (to find the maximum altitude they can ever reach)
    lsst_lat  : float
        Lattitude of the LSST site

    Returns
    -------
    alt_alloc : numpy.array

    """
    max_alt = max_altitude(dec, lsst_lat)
    normalized_alt = alt/max_alt
    if filter_name is None:
        filter_name = 'r'
    index = ['u', 'g', 'r', 'i', 'z', 'y'].index(filter_name)
    traps = np.array([0.95,0.85,0.75,0.65,0.55,0.45])

    alt_alloc = 10. * np.square(normalized_alt - traps[index])
    alt_alloc[normalized_alt >= .95] = .95
    alt_alloc[normalized_alt <= .45] = .45
    return alt_alloc


def hour_angle(ra, lsst_lon, mjd, lmst=None):
    """
    evaluates the hour angle of fields.

    Parameters
    ----------
    ra : numpy.array
        RA, in radians.
    lsst_lon  : float
        Longitude of the LSST site

    Returns
    -------
    ha : numpy.array
        Hour angle ranging from -12 to 12. Hours.
    """
    if lmst is None:
        lmst, last = calcLmstLast(mjd, lsst_lon)
    ha = lmst-ra * 12./np.pi
    ha = np.where(ha < -12, ha +24, ha)
    ha = np.where(ha > 12, ha - 24, ha)
    return ha


def mutually_exclusive_regions(nside=set_default_nside()):
    SCP_indx = SCP_healpixels(nside)
    NES_indx = NES_healpixels(nside)
    GP_indx = galactic_plane_healpixels(nside)
    all_butWFD = reduce(np.union1d, (SCP_indx, NES_indx, GP_indx))
    GP_NES     = np.union1d(GP_indx, NES_indx)

    WFD_indx = np.setdiff1d(WFD_healpixels(nside), all_butWFD, assume_unique=True)
    SCP_indx = np.setdiff1d(SCP_indx, GP_NES, assume_unique=True)
    NES_indx = np.setdiff1d(NES_indx, GP_indx, assume_unique=True)

    return SCP_indx, NES_indx, GP_indx, WFD_indx


def pix2region(indx, nside):
    ra = np.arange(hp.nside2npix(nside)); dec = np.arange(hp.nside2npix(nside))
    ra[indx], dec[indx] = _hpid2RaDec(indx)
    SE_indx = [i for i in indx if is_SE(dec[i])]
    NE_indx = [i for i in indx if (is_NE(ra[i], dec[i]) and i not in SE_indx)]
    GP_indx = [i for i in indx if (is_GP(ra[i], dec[i]) and i not in NE_indx and i not in SE_indx)]
    WFD_indx= [i for i in indx if (i not in SE_indx and i not in NE_indx and i not in GP_indx)]
    return SE_indx, NE_indx, GP_indx, WFD_indx


def is_DD(field_id): #TODO temporarily just by id, later by label or location
    if field_id in [744, 2412, 1427, 2786, 290]:
        return True
    return False

def is_SE(dec):
    SE_dec_lim = -65. # must be less than this
    if dec < SE_dec_lim:
        return True
    return False

def is_NE(ra, dec):
    NE_dec_lim = 0 # must be more than this
    NE_lat_lim = np.deg2rad(7)# must be less than this
    str_ra = str(ra * 24 / 360); str_dec = str(dec)
    Eq_body     = ephem.Equatorial(str_ra, str_dec)
    Ec_body     = ephem.Ecliptic(Eq_body)
    if dec >= NE_dec_lim and Ec_body.lat <= NE_lat_lim:
        return True
    return False

def is_GP(ra, dec):
    GP_lat_max = np.deg2rad(5)
    GP_lat_min = np.deg2rad(0)
    GP_lon_max = np.deg2rad(70)
    str_ra = str(ra * 24 / 360); str_dec = str(dec)
    Eq_body     = ephem.Equatorial(str_ra, str_dec)
    Ga_body     = ephem.Galactic(Eq_body)
    corrected_GP_lon = Ga_body.lon.real + np.deg2rad(180)
    if corrected_GP_lon > 2 * np.pi:
        corrected_GP_lon -= np.deg2rad(360)
    corrected_GP_lon -= np.deg2rad(180)
    '''
    if Ga_body.lat.real <= GP_lat_max or Ga_body.lat.real >= -GP_lat_max:
        if corrected_GP_lon <= GP_lon_max or corrected_GP_lon >= -GP_lon_max:
            return True
    return False

    '''
    if Ga_body.lat.real > GP_lat_max or Ga_body.lat.real < -GP_lat_max:
        return False
    if corrected_GP_lon > GP_lon_max or corrected_GP_lon < -GP_lon_max:
        return False
    if Ga_body.lat.real >= 0 and corrected_GP_lon >= 0:
        if Ga_body.lat.real <= GP_lat_max + float(GP_lat_min - GP_lat_max)/(GP_lon_max - 0) * Ga_body.lat.real:
            return True
        return False
    if Ga_body.lat.real < 0 and corrected_GP_lon < 0:
        if Ga_body.lat.real >= -GP_lat_max + float(-GP_lon_max + GP_lat_min)/(0 + GP_lon_max) * Ga_body.lat.real:
            return True
        return False
    if Ga_body.lat.real < 0 and corrected_GP_lon > 0:
        if Ga_body.lat.real >= -GP_lat_max + float(-GP_lat_min + GP_lat_max)/(GP_lon_max - 0) * Ga_body.lat.real:
            return True
        return False
    if Ga_body.lat.real > 0 and corrected_GP_lon < 0:
        if Ga_body.lat.real <= GP_lat_max + float(GP_lat_min - GP_lat_max)/(0 + GP_lon_max) * Ga_body.lat.real:
            return True
        return False
