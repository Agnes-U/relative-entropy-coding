import unittest
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from rec.coding.coder import GaussianCoder
from rec.coding.beam_search_coder import BeamSearchCoder
from rec.coding.samplers import RejectionSampler


class TestCoder(unittest.TestCase):
    def test_beam_search(self):
        encoder = BeamSearchCoder(kl_per_partition=6., n_carry_over=10)

        batch_t = tfp.distributions.Normal(loc=tf.constant([[5.], [-5.1]]), scale=tf.constant([[0.01], [0.01]]))
        batch_p = tfp.distributions.Normal(loc=tf.constant([[0.], [0.]]), scale=tf.constant([[1.], [1.]]))
        encoder.update_auxiliary_variance_ratios(batch_t, batch_p)
        encoder.update_auxiliary_variance_ratios(batch_t, batch_p)

        t = tfp.distributions.Normal(loc=tf.constant([[5.1]]), scale=tf.constant([[0.01]]))
        p = tfp.distributions.Normal(loc=tf.constant([[0.]]), scale=tf.constant([[1.]]))

        indices, sample = encoder.encode(t, p, seed=69420)

        # reconstructed_sample = encoder.decode(p, indices, seed=69420)
        # np.testing.assert_allclose(sample, reconstructed_sample)

    def test_rs_gaussian(self):
        sampler = RejectionSampler(sample_buffer_size=10000, r_buffer_size=1000000)
        encoder = GaussianCoder(kl_per_partition=6., sampler=sampler)

        batch_t = tfp.distributions.Normal(loc=tf.constant([[5.], [-5.1]]), scale=tf.constant([[0.01], [0.01]]))
        batch_p = tfp.distributions.Normal(loc=tf.constant([[0.], [0.]]), scale=tf.constant([[1.], [1.]]))
        encoder.update_auxiliary_variance_ratios(batch_t, batch_p)
        encoder.update_auxiliary_variance_ratios(batch_t, batch_p)

        t = tfp.distributions.Normal(loc=tf.constant([[5.1]]), scale=tf.constant([[0.01]]))
        p = tfp.distributions.Normal(loc=tf.constant([[0.]]), scale=tf.constant([[1.]]))

        encoder.encode(t, p, seed=69420, update_sampler=True)
        indices, sample = encoder.encode(t, p, seed=69420, update_sampler=False)
        codelength = encoder.get_codelength(indices)
        self.assertGreater(codelength, 0.)
        reconstructed_sample = encoder.decode(p, indices, seed=69420)
        np.testing.assert_allclose(sample, reconstructed_sample)

if __name__ == '__main__':
    unittest.main()
