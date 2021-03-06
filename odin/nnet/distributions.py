from __future__ import print_function, division, absolute_import

import numpy as np

from six import add_metaclass
from abc import ABCMeta, abstractmethod, abstractproperty

from odin import backend as K
from odin.utils.cache_utils import cache_memory
from odin.backend.role import (add_role, VariationalMean, VariationalLogsigma,
                        WEIGHT, BIAS)

from .base import NNOp


class Distribution(NNOp):
    """ Class for distribution within NN architectures """

    def __init__(self, **kwargs):
        super(Distribution, self).__init__(**kwargs)


class Normal(object):
    """ Normal """

    def __init__(self, mean, inv_std, **kwargs):
        super(Normal, self).__init__(**kwargs)


class VariationalDense(NNOp):

    def __init__(self, num_units,
                 W_init=K.rand.symmetric_uniform,
                 b_init=K.rand.constant(0),
                 activation=K.linear,
                 seed=None, **kwargs):
        super(VariationalDense, self).__init__(**kwargs)
        self.num_units = num_units
        self.W_init = W_init
        self.b_init = b_init
        # hack to prevent infinite useless loop of transpose
        self.activation = K.linear if activation is None else activation
        self.seed = seed

    # ==================== helper ==================== #
    @cache_memory # same x will return the same mean and logsigma
    def get_mean_logsigma(self, x):
        b_mean = 0. if not hasattr(self, 'b_mean') else self.b_mean
        b_logsigma = 0. if not hasattr(self, 'b_logsigma') else self.b_logsigma
        mean = self.activation(K.dot(x, self.W_mean) + b_mean)
        logsigma = self.activation(K.dot(x, self.W_logsigma) + b_logsigma)
        mean.name = 'variational_mean'
        logsigma.name = 'variational_logsigma'
        add_role(mean, VARIATIONAL_MEAN)
        add_role(logsigma, VARIATIONAL_LOGSIGMA)
        return mean, logsigma

    def sampling(self, x):
        mean, logsigma = self.get_mean_logsigma(x)
        epsilon = K.random_normal(shape=K.get_shape(mean), mean=0.0, std=1.0,
                                  dtype=mean.dtype)
        z = mean + K.exp(logsigma) * epsilon
        return z

    # ==================== abstract methods ==================== #
    def _transpose(self):
        raise NotImplementedError

    def _initialize(self):
        shape = (self.input_shape[-1], self.num_units)
        self.get_variable(initializer=self.W_init, shape=shape, name='W_mean', roles=WEIGHT)
        self.get_variable(initializer=self.W_init, shape=shape, name='W_logsigma', roles=WEIGHT)
        if self.b_init is not None:
            self.get_variable(initializer=self.b_init,
                shape=(self.num_units,), name='b_mean', roles=BIAS)
            self.get_variable(initializer=self.b_init,
                shape=(self.num_units,), name='b_logsigma', roles=BIAS)

    def _apply(self, x):
        input_shape = K.get_shape(x)
        # calculate statistics
        mean, logsigma = self.get_mean_logsigma(x)
        # variational output
        output = mean
        if K.is_training():
            output = self.sampling(x)
        # set shape for output
        K.add_shape(output, input_shape[:-1] + (self.num_units,))
        return output
