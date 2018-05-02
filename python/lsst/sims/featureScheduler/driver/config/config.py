
import numpy as np
import healpy as hp
import lsst.sims.featureScheduler as fs

target_maps = {}
nside = fs.set_default_nside(nside=32)  # Required

target_maps['u'] = fs.generate_goal_map(NES_fraction=0.,
                                        WFD_fraction=0.31*1.2, SCP_fraction=0.15,
                                        GP_fraction=0.15,
                                        WFD_upper_edge_fraction=0.0,
                                        nside=nside,
                                        generate_id_map=True)
target_maps['g'] = fs.generate_goal_map(NES_fraction=0.2,
                                        WFD_fraction=0.44*1.2, SCP_fraction=0.15,
                                        GP_fraction=0.15,
                                        WFD_upper_edge_fraction=0.0,
                                        nside=nside,
                                        generate_id_map=True)
target_maps['r'] = fs.generate_goal_map(NES_fraction=0.46,
                                        WFD_fraction=1.0*1.2, SCP_fraction=0.15,
                                        WFD_upper_edge_fraction=0.0,
                                        GP_fraction=0.15,
                                        nside=nside,
                                        generate_id_map=True)
target_maps['i'] = fs.generate_goal_map(NES_fraction=0.46,
                                        WFD_fraction=1.0*1.2, SCP_fraction=0.15,
                                        GP_fraction=0.15,
                                        WFD_upper_edge_fraction=0.0,
                                        nside=nside,
                                        generate_id_map=True)
target_maps['z'] = fs.generate_goal_map(NES_fraction=0.4,
                                        WFD_fraction=0.9*1.2, SCP_fraction=0.15,
                                        GP_fraction=0.15,
                                        WFD_upper_edge_fraction=0.0,
                                        nside=nside,
                                        generate_id_map=True)
target_maps['y'] = fs.generate_goal_map(NES_fraction=0.,
                                        WFD_fraction=0.9*1.2, SCP_fraction=0.15,
                                        GP_fraction=0.15,
                                        WFD_upper_edge_fraction=0.0,
                                        nside=nside,
                                        generate_id_map=True)

cloud_map = fs.utils.generate_cloud_map(target_maps,filtername='r',
                                        wfd_cloud_max=0.7,
                                        scp_cloud_max=0.7,
                                        gp_cloud_max=0.7,
                                        nes_cloud_max=0.7)

width = np.arange(4.,20.,1)
z_pad = width+8
weight = (4./width)**2
height = np.zeros_like(width)+80.

filters = ['u', 'g', 'r', 'i', 'z', 'y']
surveys = []


# Configure patches for HADecAltAzPatchBasisFunction
def az_w(x):
    return -x/14. + 15./14.


patches = []
for ha in np.arange(1., 2.0, 0.2):
    patches.append({'ha_min': ha, 'ha_max': 23.9,
                    'alt_max': 82., 'alt_min': 55.,
                    'dec_min': -30.2444 - 9., 'dec_max': -30.2444 + 9.,
                    'az_min': 0., 'az_max': 360.,
                    'weight': 2. - ha})


ha_range = np.arange(0.1, 2.0, 0.2)
ha_range = np.array([0.2, 0.5, 1.9])
ha_weight = np.array([1.0,0.5,0.2])
dec_min = np.zeros(len(ha_range)) - 90.
dec_max = np.zeros(len(ha_range)) - 1.
for i, ha in enumerate(ha_range):
    patches.append({'ha_min': ha, 'ha_max': 23.9,
                    'alt_max': 82., 'alt_min': 55.,
                    'dec_min': dec_min[i], 'dec_max': dec_max[i],
                    'az_min': 0., 'az_max': 360.,
                    'weight': ha_weight[i]})
patches.append({'ha_min': 0., 'ha_max': 24. - ha_range[-1],
                'alt_max': 82., 'alt_min': 55.,
                'dec_min': dec_min[i], 'dec_max': dec_max[i],
                'az_min': 0., 'az_max': 360.,
                'weight': 1e-7})

for az in np.arange(1, 15):
    patches.append({'alt_max': 82., 'alt_min': 20.,
                    'dec_min': -90, 'dec_max': 90,
                    'az_min': 0., 'az_max': az,
                    'weight': az_w(az)})
    patches.append({'alt_max': 82., 'alt_min': 20.,
                    'dec_min': -90, 'dec_max': 90,
                    'az_min': 360. - az, 'az_max': 360.,
                    'weight': az_w(az)})

    patches.append({'alt_max': 82., 'alt_min': 20.,
                    'dec_min': -90, 'dec_max': 90,
                    'az_min': 180. - az, 'az_max': 180. + az,
                    'weight': az_w(az)})

for filtername in filters:
    bfs = []
    bfs.append(fs.M5_diff_basis_function(filtername=filtername, nside=nside))
    bfs.append(fs.Target_map_basis_function(filtername=filtername,
                                            target_map=target_maps[filtername][0],
                                            out_of_bounds_val=hp.UNSEEN, nside=nside))
    bfs.append(fs.HADecAltAzPatchBasisFunction(nside=nside,
                                               patches=patches[::-1]))
    bfs.append(fs.Slewtime_basis_function(filtername=filtername, nside=nside, order=6.))
    bfs.append(fs.Strict_filter_basis_function(filtername=filtername,
                                               time_lag_min=30.,
                                               time_lag_max=60.,
                                               time_lag_boost=120.,
                                               unseen_before_lag=True))
    bfs.append(fs.Avoid_Fast_Revists(filtername=filtername, gap_min=240., nside=nside))
    bfs.append(fs.Bulk_cloud_basis_function(max_cloud_map=cloud_map,nside=nside))
    bfs.append(fs.Moon_avoidance_basis_function(nside=nside, moon_distance=33.))

    weights = np.array([3.0, 0.5, 1., 3., 1.5, 3., 3.0, 1.0])
    surveys.append(fs.Greedy_survey_fields(bfs, weights, block_size=1,
                                           filtername=filtername, dither=True,
                                           nside=nside,
                                           tag_fields=True,
                                           tag_map=target_maps[filtername][1],
                                           tag_names=target_maps[filtername][2],
                                           ignore_obs='DD'))

# Set up pairs
pairs_bfs = []

pair_map = np.zeros(len(target_maps['z'][0]))
pair_map.fill(hp.UNSEEN)
wfd = np.where(target_maps['z'][1] == 3)
nes = np.where(target_maps['z'][1] == 1)
pair_map[wfd] = 1.
pair_map[nes] = 1.

pairs_bfs.append(fs.Target_map_basis_function(filtername='',
                                              target_map=pair_map,
                                              out_of_bounds_val=hp.UNSEEN, nside=nside))
pairs_bfs.append(fs.MeridianStripeBasisFunction(nside=nside, zenith_pad=(45.,), width=(35.,)))
pairs_bfs.append(fs.Moon_avoidance_basis_function(nside=nside, moon_distance=30.))

surveys.append(fs.Pairs_survey_scripted(pairs_bfs, [1., 1., 1.], ignore_obs='DD', min_alt=20.))
# surveys.append(fs.Pairs_survey_scripted([], [], ignore_obs='DD'))

# Set up the DD
# ELAIS S1
surveys.append(fs.Deep_drilling_survey(9.45, -44., sequence='rgizy',
                                       nvis=[20, 10, 20, 26, 20],
                                       survey_name='DD:ELAISS1', reward_value=100, moon_up=None,
                                       fraction_limit=0.148, ha_limits=([0., 0.5], [23.5, 24.]),
                                       nside=nside))
surveys.append(fs.Deep_drilling_survey(9.45, -44., sequence='u',
                                       nvis=[7],
                                       survey_name='DD:u,ELAISS1', reward_value=100, moon_up=False,
                                       fraction_limit=0.0012, ha_limits=([0., 0.5], [23.5, 24.]),
                                       nside=nside))

# XMM-LSS
surveys.append(fs.Deep_drilling_survey(35.708333, -4 - 45 / 60., sequence='rgizy',
                                       nvis=[20, 10, 20, 26, 20],
                                       survey_name='DD:XMM-LSS', reward_value=100, moon_up=None,
                                       fraction_limit=0.148, ha_limits=([0., 0.5], [23.5, 24.]),
                                       nside=nside))
surveys.append(fs.Deep_drilling_survey(35.708333, -4 - 45 / 60., sequence='u',
                                       nvis=[7],
                                       survey_name='DD:u,XMM-LSS', reward_value=100, moon_up=False,
                                       fraction_limit=0.0012, ha_limits=([0., 0.5], [23.5, 24.]),
                                       nside=nside))

# Extended Chandra Deep Field South
# XXX--Note, this one can pass near zenith. Should go back and add better planning on this.
surveys.append(fs.Deep_drilling_survey(53.125, -28. - 6 / 60., sequence='rgizy',
                                       nvis=[20, 10, 20, 26, 20],
                                       survey_name='DD:ECDFS', reward_value=100, moon_up=None,
                                       fraction_limit=0.148, ha_limits=[[0.5, 1.0], [23., 22.5]],
                                       nside=nside))
surveys.append(fs.Deep_drilling_survey(53.125, -28. - 6 / 60., sequence='u',
                                       nvis=[7],
                                       survey_name='DD:u,ECDFS', reward_value=100, moon_up=False,
                                       fraction_limit=0.0012, ha_limits=[[0.5, 1.0], [23., 22.5]],
                                       nside=nside))
# COSMOS
surveys.append(fs.Deep_drilling_survey(150.1, 2. + 10. / 60. + 55 / 3600., sequence='rgizy',
                                       nvis=[20, 10, 20, 26, 20],
                                       survey_name='DD:COSMOS', reward_value=100, moon_up=None,
                                       fraction_limit=0.148, ha_limits=([0., 0.5], [23.5, 24.]),
                                       nside=nside))
surveys.append(fs.Deep_drilling_survey(150.1, 2. + 10. / 60. + 55 / 3600., sequence='u',
                                       nvis=[7], ha_limits=([0., .5], [23.5, 24.]),
                                       survey_name='DD:u,COSMOS', reward_value=100, moon_up=False,
                                       fraction_limit=0.0012,
                                       nside=nside))

# Extra DD Field, just to get to 5. Still not closed on this one
surveys.append(fs.Deep_drilling_survey(349.386443, -63.321004, sequence='rgizy',
                                       nvis=[20, 10, 20, 26, 20],
                                       survey_name='DD:290', reward_value=100, moon_up=None,
                                       fraction_limit=0.148, ha_limits=([0., 0.5], [23.5, 24.]),
                                       nside=nside))
surveys.append(fs.Deep_drilling_survey(349.386443, -63.321004, sequence='u',
                                       nvis=[7],
                                       survey_name='DD:u,290', reward_value=100, moon_up=False,
                                       fraction_limit=0.0012, ha_limits=([0., 0.5], [23.5, 24.]),
                                       nside=nside))

scheduler = fs.Core_scheduler(surveys, nside=nside)  # Required