import numpy as np
import tensorflow as tf
from tensorpack import DataFlow, ModelDescBase, StagingInput, TowerTrainer
from tensorpack.graph_builder import DataParallelBuilder, LeastLoadedDeviceSetter
from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.tfutils.tower import TowerContext, TowerFuncWrapper
from tensorpack.utils.argtools import memoized


class GANModelDesc(ModelDescBase):
    def collect_variables(self, g_scope='gen', d_scope='discrim'):
        """
        Assign self.g_vars to the parameters under scope `g_scope`,
        and same with self.d_vars.
        """
        self.g_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, g_scope)
        assert self.g_vars
        self.d_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, d_scope)
        assert self.d_vars

    def build_losses(self, logits_real, logits_fake, extra_g=0, l2_norm=0.00001):
        """D and G play two-player minimax game with value function V(G,D)

          min_G max _D V(D, G) = IE_{x ~ p_data} [log D(x)] + IE_{z ~ p_fake} [log (1 - D(G(z)))]

        Args:
            logits_real (tf.Tensor): discrim logits from real samples
            logits_fake (tf.Tensor): discrim logits from fake samples produced by generator
        """
        with tf.name_scope("GAN_loss"):
            score_real = tf.sigmoid(logits_real)
            score_fake = tf.sigmoid(logits_fake)
            tf.summary.histogram('score-real', score_real)
            tf.summary.histogram('score-fake', score_fake)

            with tf.name_scope("discrim"):
                d_loss_pos = tf.reduce_mean(
                    tf.nn.sigmoid_cross_entropy_with_logits(
                        logits=logits_real,
                        labels=tf.ones_like(logits_real)) * .7 + tf.random_uniform(
                            tf.shape(logits_real),
                            maxval=0.3
                    ),
                    name='loss_real'
                )

                d_loss_neg = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=logits_fake, labels=tf.zeros_like(logits_fake)), name='loss_fake')

                d_pos_acc = tf.reduce_mean(
                    tf.cast(score_real > 0.5, tf.float32), name='accuracy_real')

                d_neg_acc = tf.reduce_mean(
                    tf.cast(score_fake < 0.5, tf.float32), name='accuracy_fake')

                d_loss = .5 * d_loss_pos + .5 * d_loss_neg + \
                    tf.contrib.layers.apply_regularization(
                        tf.contrib.layers.l2_regularizer(l2_norm),
                        tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "discrim"))

                self.d_loss = tf.identity(d_loss, name='loss')

            with tf.name_scope("gen"):
                g_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=logits_fake, labels=tf.ones_like(logits_fake))) + \
                    tf.contrib.layers.apply_regularization(
                        tf.contrib.layers.l2_regularizer(l2_norm),
                        tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'gen'))

                g_loss = tf.identity(g_loss, name='loss')
                extra_g = tf.identity(extra_g, name='klloss')
                self.g_loss = tf.identity(g_loss + extra_g, name='final-g-loss')

            add_moving_summary(
                g_loss, extra_g, self.g_loss, self.d_loss, d_pos_acc, d_neg_acc, decay=0.)

    @memoized
    def get_optimizer(self):
        return self._get_optimizer()


class GANTrainer(TowerTrainer):
    def __init__(self, input, model):
        super(GANTrainer, self).__init__()
        assert isinstance(model, GANModelDesc), model
        inputs_desc = model.get_inputs_desc()
        # Setup input
        cbs = input.setup(inputs_desc)
        self.register_callback(cbs)
        self.model = model
        """
        We need to set tower_func because it's a TowerTrainer,
        and only TowerTrainer supports automatic graph creation for inference during training.

        If we don't care about inference during training, using tower_func is
        not needed. Just calling model.build_graph directly is OK.
        """
        # Build the graph
        self.tower_func = TowerFuncWrapper(model.build_graph, inputs_desc)
        with TowerContext('', is_training=True):
            self.tower_func(*input.get_input_tensors())
        opt = model.get_optimizer()
        # Define the training iteration
        # by default, run one d_min after one g_min
        with tf.name_scope('optimize'):
            g_min_grad = opt.compute_gradients(model.g_loss, var_list=model.g_vars)
            g_min_grad_clip = [(tf.clip_by_value(grad, -5., 5.), var) for grad, var in g_min_grad]
            g_min_train_op = opt.apply_gradients(g_min_grad_clip, name='g_op')
            with tf.control_dependencies([g_min_train_op]):
                d_min_grad = opt.compute_gradients(model.d_loss, var_list=model.d_vars)
                d_min_grad_clip = [
                    (tf.clip_by_value(grad, -5., 5.), var) for grad, var in d_min_grad]

                d_min_train_op = opt.apply_gradients(d_min_grad_clip, name='d_op')

        self.train_op = d_min_train_op


class SeparateGANTrainer(TowerTrainer):
    """ A GAN trainer which runs two optimization ops with a certain ratio."""

    def __init__(self, input, model, d_period=1, g_period=1):
        """
        Args:
            d_period(int): period of each d_opt run
            g_period(int): period of each g_opt run
        """
        super(SeparateGANTrainer, self).__init__()
        self._d_period = int(d_period)
        self._g_period = int(g_period)
        assert min(d_period, g_period) == 1

        # Setup input
        cbs = input.setup(model.get_inputs_desc())
        self.register_callback(cbs)

        # Build the graph
        self.tower_func = TowerFuncWrapper(model.build_graph, model.get_inputs_desc())
        with TowerContext('', is_training=True):
            self.tower_func(*input.get_input_tensors())

        opt = model.get_optimizer()
        with tf.name_scope('optimize'):
            self.d_min = opt.minimize(
                model.d_loss, var_list=model.d_vars, name='d_min')
            self.g_min = opt.minimize(
                model.g_loss, var_list=model.g_vars, name='g_min')

    def run_step(self):
        # Define the training iteration
        if self.global_step % (self._d_period) == 0:
            self.hooked_sess.run(self.d_min)
        if self.global_step % (self._g_period) == 0:
            self.hooked_sess.run(self.g_min)


class MultiGPUGANTrainer(TowerTrainer):
    """
    A replacement of GANTrainer (optimize d and g one by one) with multi-gpu support.
    """

    def __init__(self, nr_gpu, input, model):
        super(MultiGPUGANTrainer, self).__init__()
        assert nr_gpu > 1
        raw_devices = ['/gpu:{}'.format(k) for k in range(nr_gpu)]

        # Setup input
        input = StagingInput(input)
        cbs = input.setup(model.get_inputs_desc())
        self.register_callback(cbs)

        # Build the graph with multi-gpu replication
        def get_cost(*inputs):
            model.build_graph(*inputs)
            return [model.d_loss, model.g_loss]
        self.tower_func = TowerFuncWrapper(get_cost, model.get_inputs_desc())
        devices = [LeastLoadedDeviceSetter(d, raw_devices) for d in raw_devices]
        cost_list = DataParallelBuilder.build_on_towers(
            list(range(nr_gpu)),
            lambda: self.tower_func(*input.get_input_tensors()),
            devices)
        # Simply average the cost here. It might be faster to average the gradients
        with tf.name_scope('optimize'):
            d_loss = tf.add_n([x[0] for x in cost_list]) * (1.0 / nr_gpu)
            g_loss = tf.add_n([x[1] for x in cost_list]) * (1.0 / nr_gpu)

            opt = model.get_optimizer()
            # run one d_min after one g_min
            g_min = opt.minimize(g_loss, var_list=model.g_vars,
                                 colocate_gradients_with_ops=True, name='g_op')
            with tf.control_dependencies([g_min]):
                d_min = opt.minimize(d_loss, var_list=model.d_vars,
                                     colocate_gradients_with_ops=True, name='d_op')
        # Define the training iteration
        self.train_op = d_min


class RandomZData(DataFlow):
    def __init__(self, shape):
        super(RandomZData, self).__init__()
        self.shape = shape

    def get_data(self):
        while True:
            yield [np.random.normal(0, 1, size=self.shape)]