from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from ray.rllib.ddpg.random_process import OrnsteinUhlenbeckProcess
from ray.experimental.tfutils import TensorFlowVariables

import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim


class DDPGModel():
    def __init__(self, registry, env, config):
        self.registry = registry
        self.env = env
        self.config = config
        self.sess = tf.Session()

        with tf.variable_scope("model"):
            self.model = DDPGActorCritic(self.registry,
                                         self.env,
                                         self.config,
                                         self.sess)
        with tf.variable_scope("target_model"):
            self.target_model = DDPGActorCritic(self.registry,
                                                self.env,
                                                self.config,
                                                self.sess)
        self._setup_gradients()
        self._setup_target_updates()

        self.initialize()
        # Set initial target weights to match model weights.
        self._initialize_target_weights()

    def _setup_gradients(self):
        """Setups critic and actor gradients."""
        self.critic_grads = tf.gradients(self.model.critic_loss,
                                         self.model.critic_var_list)
        c_grads_and_vars = list(zip(self.critic_grads,
                                self.model.critic_var_list))
        c_opt = tf.train.AdamOptimizer(self.config["critic_lr"])
        self._apply_c_gradients = c_opt.apply_gradients(c_grads_and_vars)

        self.actor_grads = tf.gradients(-self.model.cn_for_loss,
                                        self.model.actor_var_list)
        a_grads_and_vars = list(zip(self.actor_grads,
                                self.model.actor_var_list))
        a_opt = tf.train.AdamOptimizer(self.config["actor_lr"])
        self._apply_a_gradients = a_opt.apply_gradients(a_grads_and_vars)

    def compute_gradients(self, samples):
        """ Returns gradient w.r.t. samples."""
        # actor gradients
        actor_actions = self.sess.run(self.model.output_action,
                                      feed_dict={
                                        self.model.obs: samples["obs"]
                                      })

        actor_feed_dict = {
            self.model.obs: samples["obs"],
            self.model.output_action: actor_actions,
        }
        self.actor_grads = [g for g in self.actor_grads if g is not None]
        actor_grad = self.sess.run(self.actor_grads, feed_dict=actor_feed_dict)

        # feed samples into target actor
        target_Q_act = self.sess.run(self.target_model.output_action,
                                     feed_dict={
                                      self.target_model.obs: samples["new_obs"]
                                     })
        target_Q_dict = {
            self.target_model.obs: samples["new_obs"],
            self.target_model.act: target_Q_act,
        }

        target_Q = self.sess.run(self.target_model.critic_eval,
                                 feed_dict=target_Q_dict)

        # critic gradients
        critic_feed_dict = {
            self.model.obs: samples["obs"],
            self.model.act: samples["actions"],
            self.model.reward: samples["rewards"],
            self.model.target_Q: target_Q,
        }
        self.critic_grads = [g for g in self.critic_grads if g is not None]
        critic_grad = self.sess.run(self.critic_grads,
                                    feed_dict=critic_feed_dict)
        return (critic_grad, actor_grad), {}

    def apply_gradients(self, grads):
        """Applies gradients to evaluator weights."""
        c_grads, a_grads = grads
        critic_feed_dict = dict(zip(self.critic_grads, c_grads))
        self.sess.run(self._apply_c_gradients, feed_dict=critic_feed_dict)
        actor_feed_dict = dict(zip(self.actor_grads, a_grads))
        self.sess.run(self._apply_a_gradients, feed_dict=actor_feed_dict)

    def _setup_target_updates(self):
        """Set up target actor and critic updates."""
        a_updates = []
        for var, target_var in zip(self.model.actor_var_list,
                                   self.target_model.actor_var_list):
            a_updates.append(
                tf.assign(target_var,
                          self.config["tau"] * var
                          + (1. - self.config["tau"]) * target_var)
            )
        actor_updates = tf.group(*a_updates)

        c_updates = []
        for var, target_var in zip(self.model.critic_var_list,
                                   self.target_model.critic_var_list):
            c_updates.append(
                tf.assign(target_var,
                          self.config["tau"] * var
                          + (1. - self.config["tau"]) * target_var)
            )
        critic_updates = tf.group(*c_updates)
        self.target_updates = [actor_updates, critic_updates]

    def update_target(self):
        """Updates target critic and target actor."""
        self.sess.run(self.target_updates)

    def initialize(self):
        self.sess.run(tf.global_variables_initializer())

    def _initialize_target_weights(self):
        a_updates = []
        for var, target_var in zip(self.model.actor_var_list,
                                   self.target_model.actor_var_list):
            a_updates.append(tf.assign(target_var, var))
        actor_updates = tf.group(*a_updates)

        c_updates = []
        for var, target_var in zip(self.model.critic_var_list,
                                   self.target_model.critic_var_list):
            c_updates.append(tf.assign(target_var, var))
        critic_updates = tf.group(*c_updates)
        self.sess.run([actor_updates, critic_updates])


class DDPGActorCritic():
    other_output = []
    is_recurrent = False

    def __init__(self, registry, env, config, sess):
        self.env = env
        self.registry = registry
        self.config = config
        self.sess = sess

        obs_space = env.observation_space
        ac_space = env.action_space

        self.obs_size = int(np.prod(obs_space.shape))
        self.obs = tf.placeholder(tf.float32, [None, self.obs_size])
        self.ac_size = int(np.prod(ac_space.shape))
        self.act = tf.placeholder(tf.float32, [None, self.ac_size])
        self.action_bound = env.action_space.high
        # TODO: change action_bound to make more general

        # set up actor network
        self._setup_actor_network(obs_space, ac_space)

        # setting up critic
        self._setup_critic_network(obs_space, ac_space)
        self._setup_critic_loss(ac_space)

        with tf.variable_scope("critic"):
            self.critic_var_list = tf.get_collection(
                                     tf.GraphKeys.TRAINABLE_VARIABLES,
                                     tf.get_variable_scope().name
                                   )
            self.critic_vars = TensorFlowVariables(self.critic_loss,
                                                   self.sess)

        with tf.variable_scope("actor"):
            self.actor_var_list = tf.get_collection(
                                    tf.GraphKeys.TRAINABLE_VARIABLES,
                                    tf.get_variable_scope().name
                                  )
            self.actor_vars = TensorFlowVariables(self.output_action,
                                                  self.sess)

        if (self.config["noise_add"]):
            params = self.config["noise_parameters"]
            self.rand_process = OrnsteinUhlenbeckProcess(size=self.ac_size,
                                                         theta=params["theta"],
                                                         mu=params["mu"],
                                                         sigma=params["sigma"])
            self.epsilon = 1.0

    def _setup_critic_loss(self, action_space):
        self.target_Q = tf.placeholder(tf.float32, [None, 1], name="target_q")

        # compare critic eval to critic_target (squared loss)
        self.reward = tf.placeholder(tf.float32, [None], name="reward")
        self.critic_target = tf.expand_dims(self.reward, 1) + \
            self.config['gamma'] * self.target_Q
        self.critic_loss = tf.reduce_mean(tf.square(
                                        self.critic_target - self.critic_eval))

    def _setup_critic_network(self, obs_space, ac_space):
        """Sets up Q network."""
        with tf.variable_scope("critic", reuse=tf.AUTO_REUSE):
            self.critic_eval = self._create_critic_network(
                               self.obs, self.act)

        with tf.variable_scope("critic", reuse=True):
            tf.get_variable_scope().reuse_variables()
            self.cn_for_loss = self._create_critic_network(
                               self.obs, self.output_action)

    def _create_critic_network(self, obs, action):
        w_normal = tf.truncated_normal_initializer()
        w_init = tf.random_uniform_initializer(minval=-0.0003, maxval=0.0003)
        net = slim.fully_connected(obs, 400, activation_fn=tf.nn.relu,
                                   weights_initializer=w_normal)
        t1 = slim.fully_connected(net, 300,
                                  activation_fn=None, biases_initializer=None,
                                  weights_initializer=w_normal)
        t2 = slim.fully_connected(action, 300, activation_fn=None,
                                  weights_initializer=w_normal)
        net = tf.nn.relu(tf.add(t1, t2))

        out = slim.fully_connected(net,
                                   1,
                                   activation_fn=None,
                                   weights_initializer=w_init)
        return out

    def _setup_actor_network(self, obs_space, ac_space):
        with tf.variable_scope("actor", reuse=tf.AUTO_REUSE):
            self.output_action = self._create_actor_network(self.obs)

    def _create_actor_network(self, obs):
        w_normal = tf.truncated_normal_initializer()
        w_init = tf.random_uniform_initializer(minval=-0.003, maxval=0.003)

        net = slim.fully_connected(obs,
                                   400,
                                   activation_fn=tf.nn.relu,
                                   weights_initializer=w_normal)
        net = slim.fully_connected(net,
                                   300,
                                   activation_fn=tf.nn.relu,
                                   weights_initializer=w_normal)
        out = slim.fully_connected(net,
                                   self.ac_size,
                                   activation_fn=tf.nn.tanh,
                                   weights_initializer=w_init)
        scaled_out = tf.multiply(out, self.action_bound)
        return scaled_out

    def get_weights(self):
        """Returns critic weights, actor weights."""
        return self.critic_vars.get_weights(), self.actor_vars.get_weights()

    def set_weights(self, weights):
        """Sets critic and actor weights."""
        critic_weights, actor_weights = weights
        self.critic_vars.set_weights(critic_weights)
        self.actor_vars.set_weights(actor_weights)

    def compute(self, ob):
        """Returns action, given state."""
        flattened_ob = np.reshape(ob, [-1, np.prod(ob.shape)])
        action = self.sess.run(self.output_action, {self.obs: flattened_ob})
        if (self.config["noise_add"]):
            action += self.epsilon * self.rand_process.sample()
            if (self.epsilon > 0):
                self.epsilon -= self.config["noise_epsilon"]
        return action[0], {}

    def value(self, *args):
        return 0
