import numpy as np
import unittest
import lsst.utils.tests
import lsst.sims.featureScheduler.basis_functions as basis_functions
from lsst.sims.featureScheduler.utils import empty_observation
from lsst.sims.featureScheduler.features import Conditions


class TestBasis(unittest.TestCase):

    def testVisit_repeat_basis_function(self):
        bf = basis_functions.Visit_repeat_basis_function()

        indx = np.array([1000])

        # 30 minute step
        delta = 30./60./24.

        # Add 1st observation, should still be zero
        obs = empty_observation()
        obs['filter'] = 'r'
        obs['mjd'] = 59000.
        conditions = Conditions()
        conditions.mjd = np.max(obs['mjd'])
        bf.add_observation(obs, indx=indx)
        self.assertEqual(np.max(bf(conditions)), 0.)

        # Advance time so now we want a pair
        conditions.mjd += delta
        self.assertEqual(np.max(bf(conditions)), 1.)

        # Now complete the pair and it should go back to zero
        bf.add_observation(obs, indx=indx)

        conditions.mjd += delta
        self.assertEqual(np.max(bf(conditions)), 0.)


class TestMemory(lsst.utils.tests.MemoryTestCase):
    pass


def setup_module(module):
    lsst.utils.tests.init()


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()
