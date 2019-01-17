import numpy as np
from lsst.sims.featureScheduler import features
from lsst.sims.featureScheduler import utils
import healpy as hp
import matplotlib.pylab as plt
import warnings
from lsst.sims.featureScheduler.basis_functions import Base_basis_function


__all__ = ["Target_map_modulo_basis_function"]


class Target_map_modulo_basis_function(Base_basis_function):
    """Basis function that tracks number of observations and tries to match a specified spatial distribution
    can enter multiple maps that will be used at different times in the survey

    Parameters
    ----------
    day_offset : np.array
        Healpix map that has the offset to be applied to each pixel when computing what season it is on.
    filtername : (string 'r')
        The name of the filter for this target map.
    nside: int (default_nside)
        The healpix resolution.
    target_maps : list of numpy array (None)
        healpix maps showing the ratio of observations desired for all points on the sky. Last map will be used
        for season -1. Probably shouldn't support going to season less than -1.
    norm_factor : float (0.00010519)
        for converting target map to number of observations. Should be the area of the camera
        divided by the area of a healpixel divided by the sum of all your goal maps. Default
        value assumes LSST foV has 1.75 degree radius and the standard goal maps. If using
        mulitple filters, see lsst.sims.featureScheduler.utils.calc_norm_factor for a utility
        that computes norm_factor.
    out_of_bounds_val : float (-10.)
        Reward value to give regions where there are no observations requested (unitless).
    season_modulo : int (2)
        The value to modulate the season by (years).
    max_season : int (None)
        For seasons higher than this value (pre-modulo), the final target map is used.

    """
    def __init__(self, day_offset=None, filtername='r', nside=None, target_maps=None,
                 norm_factor=None, out_of_bounds_val=-10., season_modulo=2, max_season=None):

        super(Target_map_modulo_basis_function, self).__init__(nside=nside, filtername=filtername)

        if norm_factor is None:
            warnings.warn('No norm_factor set, use utils.calc_norm_factor if using multiple filters.')
            self.norm_factor = 0.00010519
        else:
            self.norm_factor = norm_factor

        self.survey_features = {}
        # Map of the number of observations in filter

        # XXX--need to convert these features to track by season.
        for i, temp in enumerate(target_maps[0:-1]):
            self.survey_features['N_obs_%i' % i] = features.N_observations_season(i, filtername=filtername,
                                                                                  nside=self.nside,
                                                                                  modulo=season_modulo,
                                                                                  offset=day_offset,
                                                                                  max_season=max_season)
            # Count of all the observations taken in a season
            self.survey_features['N_obs_count_all_%i' % i] = features.N_obs_count_season(i, filtername=None,
                                                                                         season_modulo=season_modulo,
                                                                                         offset=day_offset,
                                                                                         nside=self.nside,
                                                                                         max_season=max_season)
        # Set the final one to be -1
        self.survey_features['N_obs_%i' % -1] = features.N_observations_season(-1, filtername=filtername,
                                                                               nside=self.nside,
                                                                               modulo=season_modulo,
                                                                               offset=day_offset,
                                                                               max_season=max_season)
        self.survey_features['N_obs_count_all_%i' % -1] = features.N_obs_count_season(-1, filtername=None,
                                                                                      season_modulo=season_modulo,
                                                                                      offset=day_offset,
                                                                                      nside=self.nside,
                                                                                      max_season=max_season)
        if target_maps is None:
            self.target_maps = utils.generate_goal_map(filtername=filtername, nside=self.nside)
        else:
            self.target_maps = target_maps
        # should probably actually loop over all the target maps?
        self.out_of_bounds_area = np.where(self.target_maps[0] == 0)[0]
        self.out_of_bounds_val = out_of_bounds_val
        self.result = np.zeros(hp.nside2npix(self.nside), dtype=float)
        self.all_indx = np.arange(self.result.size)

        # For computing what day each healpix is on
        if day_offset is None:
            self.day_offset = np.zeros(hp.nside2npix(self.nside), dtype=float)
        else:
            self.day_offset = day_offset

        self.season_modulo = season_modulo
        self.max_season = max_season

    def _calc_value(self, conditions, indx=None):
        """
        Parameters
        ----------
        indx : list (None)
            Index values to compute, if None, full map is computed
        Returns
        -------
        Healpix reward map
        """

        result = self.result.copy()
        if indx is None:
            indx = self.all_indx

        # Compute what season it is at each pixel
        seasons = utils.season_calc(conditions.night, offset=self.day_offset,
                                    modulo=self.season_modulo, max_season=self.max_season)

        composite_target = self.result.copy()[indx]
        composite_nobs = self.result.copy()[indx]
        composite_count_all = self.result.copy()[indx]

        composite_goal_N = self.result.copy()[indx]

        for season in np.unique(seasons):
            season_indx = np.where(seasons == season)[0]
            composite_target[season_indx] = self.target_maps[season][season_indx]
            composite_nobs[season_indx] = self.survey_features['N_obs_%i' % season].feature[season_indx]
            #composite_count_all[season_indx] = self.survey_features['N_obs_count_all_%i' % season].feature
            composite_goal_N[season_indx] = composite_target[season_indx] * self.survey_features['N_obs_count_all_%i' % season].feature * self.norm_factor

        # Find out how many observations we want now at those points
        # XXX--to remove
        #if self.survey_features['N_obs_count_all_-1'].feature > 2000:
        #    import pdb ; pdb.set_trace()
        # XXX--I think I need to composite [N_obs_count_all] as well
        #goal_N = composite_target * composite_count_all * self.norm_factor

        result[indx] = composite_goal_N - composite_nobs[indx]
        result[self.out_of_bounds_area] = self.out_of_bounds_val

        return result