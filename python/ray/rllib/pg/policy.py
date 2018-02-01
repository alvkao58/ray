from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

import gym
import ray
from ray.rllib.models.catalog import ModelCatalog


class PGPolicy():

    other_output = []
    is_recurrent = False

    def __init__(self, registry, ob_space, ac_space, config):
        name = "local"
        self.registry = registry
        self.local_steps = 0
        self.config = config
        worker_device = "/job:localhost/replica:0/task:0/cpu:0"
        self.g = tf.Graph()
        with self.g.as_default(), tf.device(worker_device):
            with tf.variable_scope(name):
                self._setup_graph(ob_space, ac_space)
            print("Setting up loss")
            self._setup_loss(ac_space)
            self._setup_gradients()
            self.initialize()

    def _setup_graph(self, ob_space, ac_space):
        self.x = tf.placeholder(tf.float32, shape=[None]+list(ob_space.shape))
        dist_class, self.logit_dim = ModelCatalog.get_action_dist(ac_space)
        self.model = ModelCatalog.get_model(
                        self.registry, self.x, self.logit_dim,
                        options=self.config["model_options"])
        self.action_logits = self.model.outputs  # logit for each action
        self.dist = dist_class(self.action_logits)
        self.sample = self.dist.sample()
        self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                          tf.get_variable_scope().name)

    def _setup_loss(self, action_space):
        if isinstance(action_space, gym.spaces.Box):
            ac_size = action_space.shape[0]
            self.ac = tf.placeholder(tf.float32, [None, ac_size], name="ac")
        elif isinstance(action_space, gym.spaces.Discrete):
            self.ac = tf.placeholder(tf.int64, [None], name="ac")
        else:
            raise NotImplemented(
                "action space" + str(type(action_space)) +
                "currently not supported")
        self.adv = tf.placeholder(tf.float32, [None], name="adv")

        log_prob = self.dist.logp(self.ac)

        # policy loss
        self.loss = -tf.reduce_sum(log_prob * self.adv)

    def _setup_gradients(self):
        self.grads = tf.gradients(self.loss, self.var_list)
        grads_and_vars = list(zip(self.grads, self.var_list))
        opt = tf.train.AdamOptimizer(self.config["lr"])
        self._apply_gradients = opt.apply_gradients(grads_and_vars)

    def initialize(self):
        self.sess = tf.Session(graph=self.g)
        self.variables = ray.experimental.TensorFlowVariables(
                            self.loss, self.sess)
        self.sess.run(tf.global_variables_initializer())

    def compute_gradients(self, samples):
        info = {}
        feed_dict = {
            self.x: samples["observations"],
            self.ac: samples["actions"],
            self.adv: samples["advantages"],
        }
        self.grads = [g for g in self.grads if g is not None]
        self.local_steps += 1
        grad = self.sess.run(self.grads, feed_dict=feed_dict)
        return grad, info

    def apply_gradients(self, grads):
        feed_dict = {self.grads[i]: grads[i]
                     for i in range(len(grads))}
        self.sess.run(self._apply_gradients, feed_dict=feed_dict)

    def get_weights(self):
        return self.variables.get_weights()

    def set_weights(self, weights):
        self.variables.set_weights(weights)

    def compute(self, ob, *args):
        action = self.sess.run([self.sample], {self.x: [ob]})
        return action[0][0], {}
