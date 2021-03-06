import numpy as np
import unittest
from lsst.sims.featureScheduler.schedulers import Core_scheduler
import lsst.sims.featureScheduler.basis_functions as basis_functions
import lsst.sims.featureScheduler.surveys as surveys
import lsst.utils.tests
from lsst.sims.featureScheduler.utils import standard_goals
from lsst.sims.featureScheduler.modelObservatory import Model_observatory


class TestCoreSched(unittest.TestCase):

    def testsched(self):
        target_map = standard_goals()['r']

        bfs = []
        bfs.append(basis_functions.M5_diff_basis_function())
        bfs.append(basis_functions.Target_map_basis_function(target_map=target_map))
        weights = np.array([1., 1])
        survey = surveys.Greedy_survey(bfs, weights)
        scheduler = Core_scheduler([survey])

        observatory = Model_observatory()

        # Check that we can update conditions
        scheduler.update_conditions(observatory.return_conditions())

        # Check that we can get an observation out
        obs = scheduler.request_observation()
        assert(obs is not None)

        # Check that we can flush the Queue
        scheduler.flush_queue()
        assert(len(scheduler.queue) == 0)

        # Check that we can add an observation
        scheduler.add_observation(obs)


class TestMemory(lsst.utils.tests.MemoryTestCase):
    pass


def setup_module(module):
    lsst.utils.tests.init()


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()
