from __future__ import absolute_import
from builtins import zip
from builtins import object
import numpy as np
from .utils import (empty_observation, set_default_nside, hp_in_lsst_fov, read_fields,
                    raster_sort, gnomonic_project_toxy, haversine, int_binned_stat,
                    max_reject)
from lsst.sims.utils import (_hpid2RaDec, _raDec2Hpid, Site, _angularSeparation,
                             _altAzPaFromRaDec, _xyz_from_ra_dec, _healbin,
                             _approx_RaDec2AltAz, _approx_altAz2RaDec)
import healpy as hp
from . import features
from . import dithering
from . import utils
from .tsp import tsp_convex
import matplotlib.pylab as plt
from scipy.spatial import cKDTree as kdtree
from scipy.stats import binned_statistic
from lsst.sims.featureScheduler.thomson import xyz2thetaphi, thetaphi2xyz
import copy
from .comcamTessellate import comcamTessellate

import logging

default_nside = None

log = logging.getLogger(__name__)


def roundx(x, y, binstart=0.1):
    """Round off to try and grid-up nearly gridded data
    """
    bins = np.arange(x.min(), x.max()+binstart, binstart)
    counts, bin_edges = np.histogram(x, bins=bins)

    # merge together bins that are nighboring and have counts
    new_bin_edges = []
    new_bin_edges.append(bin_edges[0])
    for i, b in enumerate(bin_edges[1:]):
        if (counts[i] > 0) & (counts[i-1] > 0):
            pass
        else:
            new_bin_edges.append(bin_edges[i])
    if bin_edges[-1] != new_bin_edges[-1]:
        new_bin_edges.append(bin_edges[-1])
    indx = np.digitize(x, new_bin_edges)
    new_bin_edges = np.array(new_bin_edges)
    bin_centers = (new_bin_edges[1:]-new_bin_edges[:-1])/2. + new_bin_edges[:-1]
    new_x = bin_centers[indx-1]
    return new_x




class Marching_army_survey(BaseSurvey):
    """
    """
    def __init__(self, basis_functions, basis_weights, extra_features=None, smoothing_kernel=None,
                 nside=default_nside, filtername='y', npick=40, site='LSST'):
        if nside is None:
            nside = set_default_nside()

        super(Marching_army_survey, self).__init__(basis_functions=basis_functions,
                                                   basis_weights=basis_weights,
                                                   extra_features=extra_features,
                                                   smoothing_kernel=smoothing_kernel,
                                                   nside=nside)
        if extra_features is None:
            self.extra_features = {}
            self.extra_features['mjd'] = features.Current_mjd()
        self.nside = nside
        self._set_altaz_fields()
        self.filtername = filtername
        self.npick = npick
        site = Site(name=site)
        self.lat_rad = site.latitude_rad
        self.lon_rad = site.longitude_rad

    def _set_altaz_fields(self, leafsize=10):
        """
        Have a fixed grid of alt,az pointings to use
        """
        tmp = read_fields()
        names = ['alt', 'az']
        types = [float, float]
        self.fields = np.zeros(tmp.size, dtype=list(zip(names, types)))
        self.fields['alt'] = tmp['dec']
        self.fields['az'] = tmp['RA']

        x, y, z = _xyz_from_ra_dec(self.fields['az'], self.fields['alt'])
        self.field_tree = kdtree(list(zip(x, y, z)), leafsize=leafsize,
                                 balanced_tree=False, compact_nodes=False)
        hpids = np.arange(hp.nside2npix(self.nside))
        self.reward_ra, self.reward_dec = _hpid2RaDec(self.nside, hpids)

    def _make_obs_list(self):
        """
        """
        if not self.reward_checked:
            self.reward = self.calc_reward_function()

        unmasked = np.where(self.reward != hp.UNSEEN)
        reward_alt, reward_az = _approx_RaDec2AltAz(self.reward_ra[unmasked],
                                                    self.reward_dec[unmasked],
                                                    self.lat_rad, self.lon_rad,
                                                    self.extra_features['mjd'].feature)
        x, y, z = _xyz_from_ra_dec(reward_az, reward_alt)

        # map the healpixels to field pointings
        dist, indx = self.field_tree.query(np.vstack((x, y, z)).T)
        field_rewards = self.reward[unmasked]

        unique_fields = np.unique(indx)
        bins = np.concatenate(([np.min(unique_fields)-1], unique_fields))+.5
        # XXX--note, this might make it possible to select a field that is in a masked region, but which
        # overlaps a healpixel that is unmasked. May need to pad out any masks my an extra half-FoV.
        field_rewards, be, bi = binned_statistic(indx, field_rewards, bins=bins, statistic='mean')

        # Ah, I can just find the distance to the max and take the nop npix
        unmasked_alt = self.fields['alt'][unique_fields]
        unmasked_az = self.fields['az'][unique_fields]

        field_max = np.max(np.where(field_rewards == np.max(field_rewards))[0])
        ang_distances = _angularSeparation(unmasked_az[field_max], unmasked_alt[field_max],
                                           unmasked_az, unmasked_alt)
        final_indx = np.argsort(ang_distances)[0:self.npick]

        final_alt = unmasked_alt[final_indx]
        final_az = unmasked_az[final_indx]

        final_ra, final_dec = _approx_altAz2RaDec(final_alt, final_az,
                                                  self.lat_rad, self.lon_rad,
                                                  self.extra_features['mjd'].feature)
        # Only want to send RA,Dec positions to the observatory
        # Now to sort the positions so that we raster in altitude, then az
        # if we have wrap-aroud, just project at az=0, because median will pull it the wrong way
        if final_az.max()-final_az.min() > np.pi:
            fudge = 0.
        else:
            fudge = np.median(final_az)
        coords = np.empty(final_alt.size, dtype=[('alt', float), ('az', float)])
        x, y = gnomonic_project_toxy(final_az, final_alt,
                                     fudge, np.median(final_alt))
        # Expect things to be mostly vertical in alt
        x_deg = np.degrees(x)
        x_new = roundx(x_deg, y)
        coords['alt'] = y
        coords['az'] = np.radians(x_new)

        # XXX--horrible horrible magic number xbin
        indx = raster_sort(coords, order=['az', 'alt'], xbin=np.radians(1.))
        # Now to loop over and stick all of those in a list of observations
        observations = []
        for ra, dec in zip(final_ra[indx], final_dec[indx]):
            obs = empty_observation()
            obs['RA'] = ra
            obs['dec'] = dec
            obs['filter'] = self.filtername
            obs['nexp'] = 2.
            obs['exptime'] = 30.
            observations.append(obs)
        # plt.plot(np.degrees(final_az[indx]), np.degrees(final_alt[indx]), 'o-')
        # plt.scatter(np.degrees(final_az[indx]), np.degrees(final_alt[indx]), c=field_rewards[order][0:npick][indx])

        # Could do something like look at current position and see if observation[0] or [-1] is closer to
        # the current pointing, then reverse if needed.

        return observations

    def __call__(self):
        observations = self._make_obs_list()
        return observations


class Marching_experiment(Marching_army_survey):
    def __init__(self, basis_functions, basis_weights, extra_features=None, smoothing_kernel=None,
                 nside=default_nside, filtername='y', npick=20, site='LSST'):
        if nside is None:
            nside = set_default_nside()

        super(Marching_experiment, self).__init__(basis_functions=basis_functions,
                                                  basis_weights=basis_weights,
                                                  extra_features=extra_features,
                                                  smoothing_kernel=smoothing_kernel,
                                                  npick=npick, nside=nside, filtername=filtername)

    def __call_(self):
        observations = self._make_obs_list()
        # Only selecting 20 fields by default, so let's trace out the 20, then backtrack on them
        observations.extend(observations[::-1])
        # and now to make sure we get pairs
        observations.extend(observations)
        return observations


class Marching_army_survey_pairs(Marching_army_survey):
    """Same as marching army, only repeat the block twice
    """
    def __call__(self):
        # XXX--simple "typewriter" style where it rasters, then does
        # A long slew back to the start. Could imagine doing every-other observation, then
        # the other half in reverse order, or some other more complicated style of looping back
        observations = self._make_obs_list()
        observations.extend(observations)
        return observations


class Smooth_area_survey(BaseSurvey):
    """
    Survey that selects a large area block at a time
    """
    def __init__(self, basis_functions, basis_weights, extra_features=None, filtername='r',
                 percentile_clip=90., smoothing_kernel=3.5, max_region_size=20.,
                 max_area=160., nside=default_nside):
        """
        Parameters
        ----------
        percentile_clip : 90.
            After the reward maximum is found, include any healpixels with reward value
            this percentile or higher within max_region_size
        max_area : float (160.)
            Area to try and observe per block (sq degrees).
        max_region_size : float (20.)
           How far away to consider healpixes after the reward function max is found (degrees)
        """
        if nside is None:
            nside = set_default_nside()

        # After overlap, get about 8 sq deg per pointing.

        if extra_features is None:
            self.extra_features = []
            self.extra_features.append(features.Coadded_depth(filtername=filtername,
                                                              nside=nside))
            self.extra_features[0].feature += 1e-5

        super(Smooth_area_survey, self).__init__(basis_functions=basis_functions,
                                                 basis_weights=basis_weights,
                                                 extra_features=self.extra_features,
                                                 smoothing_kernel=smoothing_kernel,
                                                 nside=nside)
        self.filtername = filtername
        pix_area = hp.nside2pixarea(nside, degrees=True)
        block_size = int(np.round(max_area/pix_area))
        self.block_size = block_size
        # Make the dithering solving object
        self.hpc = dithering.hpmap_cross(nside=nside)
        self.max_region_size = np.radians(max_region_size)
        self.nside = nside
        self.percentile_clip = percentile_clip

    def __call__(self):
        """
        Return pointings for a block of sky
        """
        if not self.reward_checked:
            reward_smooth = self.calc_reward_function()
        else:
            reward_smooth = self.reward_smooth

        # Pick the top healpixels to observe
        reward_max = np.where(reward_smooth == np.max(reward_smooth))[0].min()
        unmasked = np.where(self.reward_smooth != hp.UNSEEN)[0]
        selected = np.where(reward_smooth[unmasked] >= np.percentile(reward_smooth[unmasked],
                                                                     self.percentile_clip))
        selected = unmasked[selected]

        to_observe = np.empty(reward_smooth.size, dtype=float)
        to_observe.fill(hp.UNSEEN)
        # Only those within max_region_size of the maximum
        max_vec = hp.pix2vec(self.nside, reward_max)
        pix_in_disk = hp.query_disc(self.nside, max_vec, self.max_region_size)

        # Select healpixels that have high reward, and are within
        # radius of the maximum pixel
        # Selected pixels are above the percentile threshold and within the radius
        selected = np.intersect1d(selected, pix_in_disk)
        if np.size(selected) > self.block_size:
            order = np.argsort(reward_smooth[selected])
            selected = selected[order[-self.block_size:]]

        to_observe[selected] = self.extra_features[0].feature[selected]

        # Find the pointings that observe the given pixels, and minimize the cross-correlation
        # between pointing overlaps regions and co-added depth
        self.hpc.set_map(to_observe)
        best_fit_shifts = self.hpc.minimize()
        ra_pointings, dec_pointings, obs_map = self.hpc(best_fit_shifts, return_pointings_map=True)
        # Package up the observations.
        observations = []
        for ra, dec in zip(ra_pointings, dec_pointings):
            obs = empty_observation()
            obs['RA'] = ra
            obs['dec'] = dec
            obs['filter'] = self.filtername
            obs['nexp'] = 2.
            obs['exptime'] = 30.
            observations.append(obs)
        return observations


class Simple_greedy_survey_fields(BaseSurvey):
    """
    Chop down the reward function to just look at unmasked opsim field locations.
    """
    def __init__(self, basis_functions, basis_weights, extra_features=None, filtername='r',
                 block_size=25, smoothing_kernel=None, nside=default_nside, ignore_obs='ack'):
        if nside is None:
            nside = set_default_nside()
        super(Simple_greedy_survey_fields, self).__init__(basis_functions=basis_functions,
                                                          basis_weights=basis_weights,
                                                          extra_features=extra_features,
                                                          smoothing_kernel=smoothing_kernel,
                                                          ignore_obs=ignore_obs,
                                                          nside=nside)
        self.filtername = filtername
        self.fields = read_fields()
        self.field_hp = _raDec2Hpid(self.nside, self.fields['RA'], self.fields['dec'])
        self.block_size = block_size

    def __call__(self):
        """
        Just point at the highest reward healpix
        """
        if not self.reward_checked:
            self.reward = self.calc_reward_function()
        # Let's find the best N from the fields
        reward_fields = self.reward[self.field_hp]
        reward_fields[np.where(reward_fields.mask == True)] = -np.inf
        order = np.argsort(reward_fields)[::-1]
        best_fields = order[0:self.block_size]
        observations = []
        for field in best_fields:
            obs = empty_observation()
            obs['RA'] = self.fields['RA'][field]
            obs['dec'] = self.fields['dec'][field]
            obs['filter'] = self.filtername
            obs['field_id'] = self.fields['field_id'][field]
            obs['nexp'] = 2.
            obs['exptime'] = 30.
            observations.append(obs)
        return observations




class Block_survey(Greedy_survey_fields):
    """Select observations in blocks.
    """
    def __init__(self, basis_functions, basis_weights, alt_az_masks,
                 extra_features=None, filtername='r', filter2='g',
                 slew_approx=7.5, filter_change_approx=120.,
                 read_approx=2., exptime=30., nexp=2,
                 pair_time=22., alt_az_blockmaps=None,
                 smoothing_kernel=None, nside=default_nside,
                 dither=True, seed=42, ignore_obs='ack',
                 tag_fields=False, tag_map=None, tag_names=None,
                 sun_alt_limit=-19., survey_note='block'):
        """
        This survey computes a reward function in the usual way (linear combination
        of basis functions and weights), then takes the extra step of applying a
        series of alt,az masks to see which large block of sky has the highest reward.

        In theory, we could make a survey for each potential large block, but that
        would mean re-computing the same reward function for every possible alt,az mask.

        Parameters
        ----------
        alt_az_masks : list of numpy arrays
            Should be healpix masks, 1 for good, 0 for masked. Should be at an nside
            resolution higher than the survey nside (makes it easy to rotate and downgrade them)
        slew_approx : float (8.5)
            The approximate slew time to go to a nearby pointing. (seconds)
        filter_change_approx : float (120)
            Approx filter change time (seconds)
        read_approx : float (2.)
            The apporximate readtime of the camera (seconds)
        exptime : float (30.)
            The total exposure time of a visit (seconds)
        nexp : int (2)
            The number of exposures a visit is broken into
        pair_time : float (20.)
            The ideal gap between taking pairs of observations (minutes). If filter2
            is None, this will be the approximate time spent observing a single block
        sun_alt_limit : float (-19.)
            Don't attempt to observe if the sun is higher than this (degrees)
        survey_note : str ('')
            A note to tag the observations with.
        filter2 : str ('g')
            What filter to take the "second" in a pair with. Can be set to None to
            note take a pair. The current filter is checked to possibly minimize filter
            changes, so filter2 may be taken first.
        """

        if nside is None:
            nside = set_default_nside()

        if extra_features is None:
            extra_features = {}
            extra_features['night'] = features.Current_night()
            extra_features['mounted_filters'] = features.Mounted_filters()
            extra_features['mjd'] = features.Current_mjd()
            extra_features['night_boundaries'] = features.CurrentNightBoundaries()
            extra_features['sun_moon_alt'] = features.Sun_moon_alts()
            extra_features['lmst'] = features.Current_lmst()  # Pretty sure in hours
            extra_features['current_filter'] = features.Current_filter()
            extra_features['altaz'] = features.AltAzFeature()

        super(Block_survey, self).__init__(basis_functions=basis_functions,
                                           basis_weights=basis_weights,
                                           extra_features=extra_features,
                                           filtername=filtername,
                                           block_size=0, smoothing_kernel=smoothing_kernel,
                                           dither=dither, seed=seed, ignore_obs=ignore_obs,
                                           tag_fields=tag_fields, tag_map=tag_map,
                                           tag_names=tag_names,
                                           nside=nside)

        # Calculate how many visits we should do before going back an getting pairs
        # XXX--here's some potential for look-ahead. If we have a range of acceptable
        # pair delays, we could stretch/compress self.nvisit_block to fill the available time
        # and use less filler-time surveys. But then might also be nice to know if DD want to go
        # and check if there's scheduled downtime.
        self.nvisit_block = int(np.floor(pair_time*60. / (slew_approx + exptime + read_approx*(nexp - 1))))
        self.nexp = nexp
        self.exptime = exptime
        self.hpids = np.arange(hp.nside2npix(self.nside))
        self.alt_az_blockmaps = alt_az_blockmaps
        if filter2 is not None:
            self.time_needed = (pair_time*60.*2. + exptime + read_approx + filter_change_approx)/24./3600.  # Days
        else:
            self.time_needed = (pair_time*60. + exptime + read_approx)/24./3600.  # Days
        self.filter_set = set(filtername)
        if filter2 is None:
            self.filter2_set = self.filter_set
        else:
            self.filter2_set = set(filter2)
        self.sun_alt_limit = np.radians(sun_alt_limit)

        # Let's set up the masks we are going to use
        self.alt_az_masks = alt_az_masks
        mask_nside = hp.npix2nside(alt_az_masks[0].size)
        hpids = np.arange(alt_az_masks[0].size)
        self.az, self.alt = _hpid2RaDec(mask_nside, hpids)
        site = Site(name='LSST')
        self.lat = site.latitude_rad
        self.lon = site.longitude_rad
        self.survey_note = survey_note
        self.counter = 1  # start at 1, because 0 is default in empty observation
        self.filter2 = filter2

    def _check_feasability(self):
        # Check if filters are loaded and
        filters_mounted = self.filter_set.issubset(set(self.extra_features['mounted_filters'].feature))
        if self.filter2 is not None:
            second_fitler_mounted = self.filter2_set.issubset(set(self.extra_features['mounted_filters'].feature))
            filters_mounted = filters_mounted & second_fitler_mounted

        available_time = self.extra_features['night_boundaries'].feature['next_twilight_start'] - self.extra_features['mjd'].feature
        if not filters_mounted:
            return False
        # Check we are not in twilight
        elif self.extra_features['sun_moon_alt'].feature['sunAlt'] > self.sun_alt_limit:
            return False
        # We have enough time before twilight starts
        elif available_time < self.time_needed:
            return False
        else:
            return True

    def calc_reward_function(self):
        """
        Going to loop over all the masks and find the best one
        """
        #  Computing reward like usual
        self.reward_checked = True
        if self._check_feasability():
            self.reward = 0
            indx = np.arange(hp.nside2npix(self.nside))
            # keep track of masked pixels
            mask = np.zeros(indx.size, dtype=bool)
            for bf, weight in zip(self.basis_functions, self.basis_weights):
                basis_value = bf(indx=indx)
                mask[np.where(basis_value == hp.UNSEEN)] = True
                if hasattr(basis_value, 'mask'):
                    mask[np.where(basis_value.mask == True)] = True
                self.reward += basis_value*weight
                # might be faster to pull this out into the feasabiliity check?
                if hasattr(self.reward, 'mask'):
                    indx = np.where(self.reward.mask == False)[0]
            self.reward[mask] = hp.UNSEEN
            self.reward.mask = mask
            self.reward.fill_value = hp.UNSEEN
            # inf reward means it trumps everything.
            if np.any(np.isinf(self.reward)):
                self.reward = np.inf

            if self.smoothing_kernel is not None:
                self.smooth_reward()

            # With the full reward map, now apply the masks and see which one is the winner
            potential_rewards = []
            potential_pointings = []
            potential_reward_maps = []
            for alt_az_mask in self.alt_az_masks:
                # blank reward map
                reward_map = self.reward*0 + hp.UNSEEN
                # convert the alt,az mask to ra,dec like the reward
                alt = self.extra_features['altaz'].feature['alt']
                az = self.extra_features['altaz'].feature['az']
                mask_to_radec = hp.get_interp_val(alt_az_mask, np.pi/2 - alt, az)
                # Potential healpixels to observe, unmasked in alt,az and reward function
                good = np.where((self.reward != hp.UNSEEN) & (mask_to_radec > 0.))
                reward_map[good] = self.reward[good]
                potential_reward_maps.append(reward_map)
                # All the potential pointings in the alt,az block
                fields = self.hp2fields[self.hpids[good]]
                # compute a reward for each of the potential fields
                ufields = np.unique(fields)
                # Gather all the healpixels that are available
                observed_hp = np.isin(self.hp2fields, ufields)
                # now to bin up the reward for each field pointing
                # Use the max reward, I think summing ran into issues where
                # some observations could have more/fewer pixels. So should use max or mean.
                ufields, reward_by_field = int_binned_stat(self.hp2fields[observed_hp],
                                                           self.reward[observed_hp].filled(),
                                                           statistic=max_reject)
                # toss any -inf
                good_fields = np.where(reward_by_field > -np.inf)
                ufields = ufields[good_fields]
                reward_by_field = reward_by_field[good_fields]
                # if we don't have enough fields to observe a block, mark as -inf
                if np.size(reward_by_field) < self.nvisit_block:
                    potential_pointings.append(0)
                    potential_rewards.append(-np.inf)
                else:
                    # Sort to take the top N field pointings
                    field_order = np.argsort(reward_by_field)[::-1][0:self.nvisit_block]
                    potential_pointings.append(ufields[field_order])
                    potential_rewards.append(np.sum(reward_by_field[field_order]))
            # Pick the best pointing blob
            best_blob = np.min(np.where(potential_rewards == np.max(potential_rewards))[0])
            self.best_fields = potential_pointings[best_blob]
            # Let's return the winning map. Should make it easier to investigate what's going on
            # Next level up will take the max value from this to decide to execute or not.
            # I think this will make it possible to balance filters like before.
            self.reward = potential_reward_maps[best_blob]
            return self.reward
        else:
            # If not feasable, negative infinity reward
            self.reward = -np.inf
            return self.reward

    def __call__(self):
        """
        Find a good block of observations.
        """
        if not self.reward_checked:
            # This should set self.best_fields
            self.reward = self.calc_reward_function()

        # Let's find the alt, az coords of the points (right now, hopefully doesn't change much in time block)
        pointing_alt, pointing_az = _approx_RaDec2AltAz(self.fields['RA'][self.best_fields],
                                                        self.fields['dec'][self.best_fields],
                                                        self.lat, self.lon,
                                                        self.extra_features['mjd'].feature,
                                                        lmst=self.extra_features['lmst'].feature)
        # Let's find a good spot to project the points to a plane
        mid_alt = (np.max(pointing_alt) - np.min(pointing_alt))/2.

        # Code snippet from MAF for computing mean of angle accounting for wrap around
        # XXX-TODO: Maybe move this to sims_utils as a generally useful snippet.
        x = np.cos(pointing_az)
        y = np.sin(pointing_az)
        meanx = np.mean(x)
        meany = np.mean(y)
        angle = np.arctan2(meany, meanx)
        radius = np.sqrt(meanx**2 + meany**2)
        mid_az = angle % (2.*np.pi)
        if radius < 0.1:
            mid_az = np.pi

        # Project the alt,az coordinates to a plane. Could consider scaling things to represent
        # time between points rather than angular distance.
        pointing_x, pointing_y = gnomonic_project_toxy(pointing_az, pointing_alt, mid_az, mid_alt)
        # Now I have a bunch of x,y pointings. Drop into TSP solver to get an effiencent route
        towns = np.vstack((pointing_x, pointing_y)).T
        # Leaving optimize=False for speed. The optimization step doesn't usually improve much.
        better_order = tsp_convex(towns, optimize=False)
        # XXX-TODO: Could try to roll better_order to start at the nearest/fastest slew from current position.

        observations = []
        counter2 = 0
        for indx in better_order:
            field = self.best_fields[indx]
            if self.tag_fields:
                tag = np.unique(self.tag_map[np.where(self.hp2fields == field)])[0]
            else:
                tag = 1
            if tag == 0:
                continue
            obs = empty_observation()
            obs['RA'] = self.fields['RA'][field]
            obs['dec'] = self.fields['dec'][field]
            obs['rotSkyPos'] = 0.
            obs['filter'] = self.filtername
            obs['nexp'] = self.nexp
            obs['exptime'] = self.exptime
            obs['field_id'] = -1
            if self.tag_fields:
                obs['survey_id'] = np.unique(self.tag_map[np.where(self.hp2fields == field)])[0]
            else:
                obs['survey_id'] = 1
            obs['note'] = '%s' % (self.survey_note)
            obs['block_id'] = self.counter
            observations.append(obs)
            counter2 += 1

        # If we only want one filter block
        if self.filter2 is None:
            result = observations
        else:
            # Double the list to get a pair.
            observations_paired = []
            for observation in observations:
                obs = copy.copy(observation)
                obs['filter'] = self.filter2
                observations_paired.append(obs)

            # Check loaded filter here to decide which goes first
            if self.extra_features['current_filter'].feature == self.filter2:
                result = observations_paired + observations
            else:
                result = observations + observations_paired

            # Let's tag which one is supposed to be first/second in the pair:
            for i in range(0, self.nvisit_block, 1):
                result[i]['note'] = '%s, a' % (self.survey_note)
            for i in range(self.nvisit_block, self.nvisit_block*2, 1):
                result[i]['note'] = '%s, b' % (self.survey_note)

        # XXX--note, we could run this like the DD surveys and keep the queue locally,
        # then we can control how to recover if interupted, cut a sequence short if needed, etc.
        # But that is a lot of code for what should be a minor improvement.

        # Keep track of which block we're on. Nice for debugging.
        self.counter += 1
        return result


def bearing(ra1, dec1, ra2, dec2):
    """Compute the bearing between two points, input in radians
    """
    delta_lon = ra2 - ra1
    result = -np.arctan2(np.sin(delta_lon)*np.cos(dec2),
                         np.cos(dec1)*np.sin(dec2)-np.sin(dec1)*np.cos(dec2)*np.cos(delta_lon))
    result += 2.*np.pi
    result = result % (2.*np.pi)
    return result


class Greedy_survey_comcam(Greedy_survey_fields):
    """Tessellate the sky with comcam, then select pointings based on that.
    """
    def __init__(self, basis_functions, basis_weights, extra_features=None, filtername='r',
                 block_size=25, smoothing_kernel=None, nside=default_nside,
                 dither=False, seed=42, ignore_obs='ack', side_length=0.7):
        if nside is None:
            nside = set_default_nside()

        if extra_features is None:
            extra_features = {}
            extra_features['night'] = features.Current_night()
            extra_features['mounted_filters'] = features.Mounted_filters()
        super(Greedy_survey_comcam, self).__init__(basis_functions=basis_functions,
                                                   basis_weights=basis_weights,
                                                   extra_features=extra_features,
                                                   filtername=filtername,
                                                   block_size=block_size, smoothing_kernel=smoothing_kernel,
                                                   dither=dither, seed=seed, ignore_obs=ignore_obs,
                                                   nside=nside)
        self.filtername = filtername
        # Load the comcam field tesselation
        ras, decs = comcamTessellate(side_length=side_length, overlap=0.11)
        self.fields = np.zeros(ras.size, dtype=list(zip(['RA', 'dec', 'rotSkyPos'],
                                                        [float, float, float])))
        self.fields['RA'] = ras
        self.fields['dec'] = decs
        # Make an array to track the upper edge of the raft
        self.fields_edge = np.zeros(ras.size, dtype=list(zip(['RA', 'dec'], [float, float])))
        self.fields_edge['RA'] = self.fields['RA'] + 0.
        self.fields_edge['dec'] = self.fields['dec'] + np.radians(side_length)
        # If we wrapped over the north pole
        wrapped = np.where(self.fields_edge['dec'] > np.pi/2.)
        self.fields_edge['dec'][wrapped] = np.pi/2. - (self.fields_edge['dec'][wrapped] % np.pi/2)
        self.fields_edge['RA'][wrapped] = (self.fields_edge['RA'][wrapped] + np.pi) % (2.*np.pi)

        self.block_size = block_size
        self._hp2fieldsetup(self.fields['RA'], self.fields['dec'])
        np.random.seed(seed)
        self.dither = dither
        self.night = extra_features['night'].feature + 0

    def _spin_fields(self, lon=None, lat=None):
        """Spin the field tessellation
        """
        if lon is None:
            lon = np.random.rand()*np.pi*2
        if lat is None:
            lat = np.random.rand()*np.pi*2
        # rotate longitude
        ra = (self.fields['RA'] + lon) % (2.*np.pi)
        dec = self.fields['dec'] + 0

        ra_edge = (self.fields_edge['RA'] + lon) % (2.*np.pi)
        dec_edge = self.fields_edge['dec'] + 0

        # Now to rotate ra and dec about the x-axis
        x, y, z = thetaphi2xyz(ra, dec+np.pi/2.)
        xp, yp, zp = rotx(lat, x, y, z)
        theta, phi = xyz2thetaphi(xp, yp, zp)
        dec = phi - np.pi/2
        ra = theta + np.pi

        self.fields['RA'] = ra
        self.fields['dec'] = dec

        # Rotate the upper raft edge
        x, y, z = thetaphi2xyz(ra_edge, dec_edge+np.pi/2.)
        xp, yp, zp = rotx(lat, x, y, z)
        theta, phi = xyz2thetaphi(xp, yp, zp)
        dec_edge = phi - np.pi/2
        ra_edge = theta + np.pi

        self.fields_edge['RA'] = ra_edge
        self.fields_edge['dec'] = dec_edge
        # There's probably a more elegant way to do this. The rotSkyPos is
        # probably something like lat*sin(ra) but I'm too lazy to figure it out.
        self.fields['rotSkyPos'] = bearing(self.fields['RA'], self.fields['dec'],
                                           self.fields_edge['RA'], self.fields_edge['dec'])

        # Rebuild the kdtree with the new positions
        # XXX-may be doing some ra,dec to conversions xyz more than needed.
        self._hp2fieldsetup(ra, dec)

    def __call__(self):
        """
        Just point at the highest reward healpix
        """
        if not self.reward_checked:
            self.reward = self.calc_reward_function()
        # Let's find the best N from the fields
        order = np.argsort(self.reward)[::-1]
        best_hp = order[0:self.block_size]
        best_fields = np.unique(self.hp2fields[best_hp])
        observations = []
        for field in best_fields:
            obs = empty_observation()
            obs['RA'] = self.fields['RA'][field]
            obs['dec'] = self.fields['dec'][field]
            obs['filter'] = self.filtername
            obs['nexp'] = 2.
            obs['exptime'] = 30.
            # XXX-TODO: not all rotSkyPos are possible, need to add feature
            # that tracks min/max rotSkyPos, (and current rotSkyPos given rotTelPos)
            # then set rotSkyPos to the closest possible given that there can be 90 degree
            # shifts.
            obs['rotSkyPos'] = self.fields['rotSkyPos'][field]
            observations.append(obs)
        return observations


def wrapHA(HA):
    """Make sure Hour Angle is between 0 and 24 hours """
    return HA % 24.





class Pairs_different_filters_scripted(Pairs_survey_scripted):

    def __init__(self, basis_functions, basis_weights, extra_features=None, filt_to_pair='griz',
                 dt=40., ttol=10., reward_val=101., note='scripted', ignore_obs='ack',
                 min_alt=30., max_alt=85., lat=-30.2444, moon_distance=30., max_slew_to_pair=15.,
                 nside=default_nside, filter_goals=None):

        super(Pairs_different_filters_scripted, self).__init__(basis_functions, basis_weights, extra_features,
                                                               filt_to_pair, dt, ttol, reward_val,
                                                               note, ignore_obs, min_alt, max_alt, lat,
                                                               moon_distance, max_slew_to_pair, nside)

        for filtername in self.filt_to_pair:
            self.extra_features['N_obs_%s' % filtername] = features.N_obs_count(filtername=filtername)

        self.extra_features['N_obs'] = features.N_obs_count(filtername=None)
        self.filter_goals = filter_goals
        self.filter_idx = 0

    def __call__(self):
        # Toss anything in the queue that is too old to pair up:
        self._purge_queue()
        # Check for something I want a pair of
        result = []
        # if len(self.observing_queue) > 0:

        for indx in range(len(self.observing_queue)):

            check = self._check_observation(self.observing_queue[indx])

            if check[0]:
                result = self.observing_queue.pop(indx)
                result['note'] = 'pair(%s)' % self.note
                # Make sure we are in a different filter and change it to the one with the highest need if need
                if ((self.extra_features['current_filter'].feature is not None) and
                        (self.extra_features['current_filter'].feature == result['filter'])):
                    # check which filter needs more observations
                    proportion = np.zeros(len(self.filt_to_pair))
                    for i, obs_filter in enumerate(self.filt_to_pair):
                        if self.extra_features['current_filter'].feature == obs_filter:
                            proportion[i] = -1
                        else:
                            nobs = self.extra_features['N_obs_%s' % obs_filter].feature
                            nobs_all = self.extra_features['N_obs'].feature
                            goal = self.filter_goals[obs_filter]
                            proportion[i] = 1. - nobs / nobs_all + goal if nobs_all > 0 else 1. + goal
                        # proportion[i] = self.extra_features['N_obs_%s' % obs_filter].feature / \
                        #                 self.extra_features['N_obs'].feature / self.filter_goals[obs_filter]
                    self.filter_idx = np.argmax(proportion)
                    log.debug('Swapping filter to pair {} -> {}'.format(self.extra_features['current_filter'].feature,
                                                                        self.filt_to_pair[self.filter_idx]))
                    result['filter'] = self.filt_to_pair[self.filter_idx]
                else:
                    result['filter'] = self.filt_to_pair[self.filter_idx]
                # Make sure it is observable!
                # if self._check_mask(result):
                result = [result]
                break
            elif not check[1]:
                # If this is not in time window and queue is chronological, none will be...
                break

        return result




class Vary_expt_survey(Greedy_survey_fields):
    """Select observations in large, blobs. Vary exposure times so depth is fairly uniform.
    """
    def __init__(self, basis_functions, basis_weights,
                 extra_features=None, extra_basis_functions=None,
                 filtername='r', filter2='g',
                 slew_approx=7.5, filter_change_approx=140.,
                 read_approx=2., nexp=2, min_exptime=15.,
                 max_exptime=60.,
                 ideal_pair_time=22., min_pair_time=15.,
                 search_radius=30., alt_max=85., az_range=90.,
                 smoothing_kernel=None, nside=default_nside,
                 dither=True, seed=42, ignore_obs='ack',
                 tag_fields=False, tag_map=None, tag_names=None,
                 sun_alt_limit=-19., survey_note='blob',
                 sitename='LSST'):
        """
        Parameters
        ----------
        min_exp_time : float (15.)
            The minimum exposure time to use (seconds). This is the exposure time when
            conditions are dark time on the meridian (or better).
        max_exp_time : float (60.)
            The maximum exposure time to use (seconds)
        filtername : str ('r')
            The filter to observe in.
        filter2 : str ('g')
            The filter to pair with the first observation. If set to None, no pair
            will be observed.
        slew_approx : float (7.5)
            The approximate slewtime between neerby fields (seconds). Used to calculate
            how many observations can be taken in the desired time block.
        filter_change_approx : float (140.)
             The approximate time it takes to change filters (seconds).
        ideal_pair_time : float (22.)
            The ideal time gap wanted between observations to the same pointing (minutes)
        min_pair_time : float (15.)
            The minimum acceptable pair time (minutes)
        search_radius : float (30.)
            The radius around the reward peak to look for additional potential pointings (degrees)
        alt_max : float (85.)
            The maximum altitude to include (degrees).
        az_range : float (90.)
            The range of azimuths to consider around the peak reward value (degrees).
        sitename : str ('LSST')
            The name of the site to lookup latitude and longitude.
        """

        if nside is None:
            nside = set_default_nside()

        if extra_features is None:
            extra_features = {}
            extra_features['night'] = features.Current_night()
            extra_features['mounted_filters'] = features.Mounted_filters()
            extra_features['mjd'] = features.Current_mjd()
            extra_features['night_boundaries'] = features.CurrentNightBoundaries()
            extra_features['sun_moon_alt'] = features.Sun_moon_alts()
            extra_features['lmst'] = features.Current_lmst()  # Pretty sure in hours
            extra_features['current_filter'] = features.Current_filter()
            extra_features['altaz'] = features.AltAzFeature()
        if extra_basis_functions is None:
            extra_basis_functions = {}
            extra_basis_functions['filter1_m5diff'] = basis_functions.M5_diff_basis_function(filtername=filtername,
                                                                                             nside=nside)
            if filter2 is not None:
                extra_basis_functions['filter2_m5diff'] = basis_functions.M5_diff_basis_function(filtername=filter2,
                                                                                                 nside=nside)

        super(Vary_expt_survey, self).__init__(basis_functions=basis_functions,
                                               basis_weights=basis_weights,
                                               extra_features=extra_features,
                                               extra_basis_functions=extra_basis_functions,
                                               filtername=filtername,
                                               block_size=0, smoothing_kernel=smoothing_kernel,
                                               dither=dither, seed=seed, ignore_obs=ignore_obs,
                                               tag_fields=tag_fields, tag_map=tag_map,
                                               tag_names=tag_names,
                                               nside=nside)
        self.nexp = nexp
        self.min_exptime = min_exptime
        self.max_exptime = max_exptime
        self.slew_approx = slew_approx
        self.read_approx = read_approx
        self.hpids = np.arange(hp.nside2npix(self.nside))
        # If we are taking pairs in same filter, no need to add filter change time.
        if filtername == filter2:
            filter_change_approx = 0
        # Compute the minimum time needed to observe a blob (or observe, then repeat.)
        if filter2 is not None:
            self.time_needed = (min_pair_time*60.*2. + read_approx + filter_change_approx)/24./3600.  # Days
        else:
            self.time_needed = (min_pair_time*60. + read_approx)/24./3600.  # Days
        self.filter_set = set(filtername)
        if filter2 is None:
            self.filter2_set = self.filter_set
        else:
            self.filter2_set = set(filter2)
        self.sun_alt_limit = np.radians(sun_alt_limit)

        self.ra, self.dec = _hpid2RaDec(self.nside, self.hpids)
        # Look up latitude and longitude for alt,az conversions later
        # XXX: TODO: lat and lon should be in the Observatory feature. But that feature
        # needs documentation on what's in it!
        site = Site(name=sitename)
        self.lat = site.latitude_rad
        self.lon = site.longitude_rad
        self.survey_note = survey_note
        self.counter = 1  # start at 1, because 0 is default in empty observation
        self.filter2 = filter2
        self.search_radius = np.radians(search_radius)
        self.az_range = np.radians(az_range)
        self.alt_max = np.radians(alt_max)
        self.min_pair_time = min_pair_time
        self.ideal_pair_time = ideal_pair_time

    def _set_block_time(self):
        """
        Update the block size if it's getting near the end of the night.
        """

        available_time = self.extra_features['night_boundaries'].feature['next_twilight_start'] -\
                         self.extra_features['mjd'].feature
        available_time *= 24.*60.  # to minutes

        n_ideal_blocks = available_time / self.ideal_pair_time
        if n_ideal_blocks >= 3:
            self.block_time = self.ideal_pair_time
        else:
            # Now we can stretch or contract the block size to allocate the remainder time until twilight starts
            # We can take the remaining time and try to do 1,2, or 3 blocks.
            possible_times = available_time / np.arange(1, 4)
            diff = np.abs(self.ideal_pair_time-possible_times)
            best_block_time = possible_times[np.where(diff == np.min(diff))]
            self.block_time = best_block_time

    def _check_feasability(self):
        # Check if filters are loaded
        filters_mounted = self.filter_set.issubset(set(self.extra_features['mounted_filters'].feature))
        if self.filter2 is not None:
            second_fitler_mounted = self.filter2_set.issubset(set(self.extra_features['mounted_filters'].feature))
            filters_mounted = filters_mounted & second_fitler_mounted

        available_time = self.extra_features['night_boundaries'].feature['next_twilight_start'] - self.extra_features['mjd'].feature
        if not filters_mounted:
            return False
        # Check we are not in twilight
        elif self.extra_features['sun_moon_alt'].feature['sunAlt'] > self.sun_alt_limit:
            return False
        # We have enough time before twilight starts
        elif available_time < self.time_needed:
            return False
        else:
            return True

    def calc_reward_function(self):
        """

        """
        # Figure out how much time we have for observing
        self._set_block_time()

        #  Computing reward like usual with basis functions and weights
        if self._check_feasability():
            self.reward = 0
            indx = np.arange(hp.nside2npix(self.nside))
            # keep track of masked pixels
            mask = np.zeros(indx.size, dtype=bool)
            for bf, weight in zip(self.basis_functions, self.basis_weights):
                basis_value = bf(indx=indx)
                mask[np.where(basis_value == hp.UNSEEN)] = True
                if hasattr(basis_value, 'mask'):
                    mask[np.where(basis_value.mask == True)] = True
                self.reward += basis_value*weight
                # might be faster to pull this out into the feasabiliity check?
                if hasattr(self.reward, 'mask'):
                    indx = np.where(self.reward.mask == False)[0]
            self.reward[mask] = hp.UNSEEN
            self.reward.mask = mask
            self.reward.fill_value = hp.UNSEEN

            if self.smoothing_kernel is not None:
                self.smooth_reward()

            # Apply max altitude cut
            too_high = np.where(self.extra_features['altaz'].feature['alt'] > self.alt_max)
            self.reward[too_high] = hp.UNSEEN

            # Select healpixels within some radius of the max
            # This is probably faster with a kd-tree.
            peak_reward = np.min(np.where(self.reward == np.max(self.reward))[0])
            # Apply radius selection
            dists = haversine(self.ra[peak_reward], self.dec[peak_reward], self.ra, self.dec)
            out_hp = np.where(dists > self.search_radius)
            self.reward[out_hp] = hp.UNSEEN

            # Apply az cut
            az_centered = self.extra_features['altaz'].feature['az'] - self.extra_features['altaz'].feature['az'][peak_reward]
            az_centered[np.where(az_centered < 0)] += 2.*np.pi

            az_out = np.where((az_centered > self.az_range/2.) & (az_centered < 2.*np.pi-self.az_range/2.))
            self.reward[az_out] = hp.UNSEEN
            potential_hp = np.where(self.reward.filled() != hp.UNSEEN)
            # Find the max reward for each potential pointing
            ufields, reward_by_field = int_binned_stat(self.hp2fields[potential_hp],
                                                       self.reward[potential_hp].filled(),
                                                       statistic=max_reject)
            ufields, m5_filter1_by_field = int_binned_stat(self.hp2fields[potential_hp],
                                                           self.extra_basis_functions['filter1_m5diff']()[potential_hp],
                                                           statistic=np.mean)
            order = np.argsort(reward_by_field)
            # Highest reward first
            ufields = ufields[order][::-1]
            m5_filter1_by_field = m5_filter1_by_field[order][::-1]
            # calculate the exposure times that we would want, given the m5 diff values
            exptime_f1 = utils.scale_exptime(m5_filter1_by_field, self.min_exptime, self.max_exptime)
            overhead = np.zeros(exptime_f1.size) + self.slew_approx + self.read_approx*(self.nexp-1)
            elapsed_sec = np.cumsum(exptime_f1 + overhead)
            max_indx = np.max(np.where(elapsed_sec/60. <= self.block_time))
            self.exptimes_f1 = exptime_f1[0:max_indx+1]

            if self.filter2 is not None:
                # Now, we're assuming the 5-sigma depth doesn't change much over the pair period.
                # Obviously, if the moon rises or something, this is ont a good approx
                # Also, assuming the exptimes will scale similarly between filter1 and filter2.
                # If that is not the case, may miss target block time by a lot. 
                ufields, m5_filter2_by_field = int_binned_stat(self.hp2fields[potential_hp],
                                                               self.extra_basis_functions['filter2_m5diff']()[potential_hp],
                                                               statistic=np.mean)
                m5_filter2_by_field[order][::-1]
                exptime_f2 = utils.scale_exptime(m5_filter2_by_field, self.min_exptime, self.max_exptime)
                self.exptimes_f2 = exptime_f2[0:max_indx+1]

            self.best_fields = ufields[0:max_indx+1]
        else:
            self.reward = -np.inf
        self.reward_checked = True
        return self.reward

    def __call__(self):
        """
        Find a good block of observations.
        """
        if not self.reward_checked:
            # This should set self.best_fields
            self.reward = self.calc_reward_function()

        # Let's find the alt, az coords of the points (right now, hopefully doesn't change much in time block)
        pointing_alt, pointing_az = _approx_RaDec2AltAz(self.fields['RA'][self.best_fields],
                                                        self.fields['dec'][self.best_fields],
                                                        self.lat, self.lon,
                                                        self.extra_features['mjd'].feature,
                                                        lmst=self.extra_features['lmst'].feature)
        # Let's find a good spot to project the points to a plane
        mid_alt = (np.max(pointing_alt) - np.min(pointing_alt))/2.

        # Code snippet from MAF for computing mean of angle accounting for wrap around
        # XXX-TODO: Maybe move this to sims_utils as a generally useful snippet.
        x = np.cos(pointing_az)
        y = np.sin(pointing_az)
        meanx = np.mean(x)
        meany = np.mean(y)
        angle = np.arctan2(meany, meanx)
        radius = np.sqrt(meanx**2 + meany**2)
        mid_az = angle % (2.*np.pi)
        if radius < 0.1:
            mid_az = np.pi

        # Project the alt,az coordinates to a plane. Could consider scaling things to represent
        # time between points rather than angular distance.
        pointing_x, pointing_y = gnomonic_project_toxy(pointing_az, pointing_alt, mid_az, mid_alt)
        # Now I have a bunch of x,y pointings. Drop into TSP solver to get an effiencent route
        towns = np.vstack((pointing_x, pointing_y)).T
        # Leaving optimize=False for speed. The optimization step doesn't usually improve much.
        better_order = tsp_convex(towns, optimize=False)
        # XXX-TODO: Could try to roll better_order to start at the nearest/fastest slew from current position.
        observations = []
        counter2 = 0
        for indx in better_order:
            field = self.best_fields[indx]
            if self.tag_fields:
                tag = np.unique(self.tag_map[np.where(self.hp2fields == field)])[0]
            else:
                tag = 1
            if tag == 0:
                continue
            obs = empty_observation()
            obs['RA'] = self.fields['RA'][field]
            obs['dec'] = self.fields['dec'][field]
            obs['rotSkyPos'] = 0.
            obs['filter'] = self.filtername
            obs['nexp'] = self.nexp
            obs['exptime'] = self.exptimes_f1[indx]
            obs['field_id'] = -1
            if self.tag_fields:
                obs['survey_id'] = np.unique(self.tag_map[np.where(self.hp2fields == field)])[0]
            else:
                obs['survey_id'] = 1
            obs['note'] = '%s' % (self.survey_note)
            obs['block_id'] = self.counter
            obs['note'] = '%s, a' % (self.survey_note)
            observations.append(obs)
            counter2 += 1

        # If we only want one filter block
        if self.filter2 is None:
            result = observations
        else:
            # Double the list to get a pair.
            observations_paired = []
            for i, observation in enumerate(observations):
                obs = copy.copy(observation)
                obs['filter'] = self.filter2
                obs['exptime'] = self.exptimes_f2[better_order[i]]
                obs['note'] = '%s, b' % (self.survey_note)
                observations_paired.append(obs)

            # Check loaded filter here to decide which goes first
            if self.extra_features['current_filter'].feature == self.filter2:
                result = observations_paired + observations
            else:
                result = observations + observations_paired

        # Keep track of which block we're on. Nice for debugging.
        self.counter += 1
        return result


class Scripted_single_block_survey(BaseSurvey):
    """
    Take a set of scheduled observations and serve them up.
    This executes one block of observations. If you have multiple blocks, use
    multiple Scripted)survey objects, or make a more complicated class.
    """
    def __init__(self, mjd_target, mjd_tol, extra_features=None,
                 smoothing_kernel=None, reward=1e6, ignore_obs='dummy',
                 nside=default_nside):
        """
        Parameters
        ----------
        mjd_target : float
            The MJD that the script should be executed at (days). It is up to the
            user to make sure the target is visible at that time.
        mjd_tol : float
            The tolerance on the MJD (minutes).
        reward : float (1e6)
            The reward to report if the current MJD is within mjd_tol of mjd_target.
        """
        if nside is None:
            nside = set_default_nside()

        self.mjd_target = mjd_target
        self.mjd_tol = mjd_tol/60./24.

        self.nside = nside
        self.reward_val = reward
        self.reward = -reward
        if extra_features is None:
            extra_features = {'mjd': features.Current_mjd()}
            extra_features['altaz'] = features.AltAzFeature(nside=nside)
        super(Scripted_survey, self).__init__(basis_functions=[],
                                              basis_weights=[],
                                              extra_features=extra_features,
                                              smoothing_kernel=smoothing_kernel,
                                              ignore_obs=ignore_obs,
                                              nside=nside)

    def add_observation(self, observation, indx=None, **kwargs):
        pass

    def calc_reward_function(self):
        """If it is close enough to taget time execute it, otherwise, -inf
        """
        dt = self.mjd_target - self.extra_features['mjd'].feature
        if (np.abs(dt) > self.mjd_tol) | (len(self.obs_wanted) == 0):
            self.reward = -np.inf
        else:
            self.reward = self.reward_val
        return self.reward

    def _slice2obs(self, obs_row):
        """take a slice and return a full observation object
        """
        observation = empty_observation()
        for key in ['RA', 'dec', 'filter', 'exptime', 'nexp', 'note', 'field_id']:
            observation[key] = obs_row[key]
        return observation

    def set_script(self, obs_wanted):
        """
        Parameters
        ----------
        obs_wanted : np.array
            The observations that should be executed. Needs to have columns with dtype names:
            'RA', 'dec', 'filter', 'exptime', 'nexp', 'note', 'field_id'
        """
        self.obs_wanted = [self._slice2obs(obs) for obs in obs_wanted]
        # Set something to record when things have been observed
        self.obs_log = np.zeros(obs_wanted.size, dtype=bool)

    def __call__(self):
        if self.calc_reward_function() == self.reward:
            return self.obs_wanted
        else:
            return [None]