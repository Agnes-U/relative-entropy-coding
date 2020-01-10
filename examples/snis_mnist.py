from rec.models.mnist_vae import MNISTVAE
from rec.core.modules.snis_distribution import SNISDistribution
from rec.core.utils import setup_logger

import datetime
import argparse
import logging

import tensorflow as tf
tfs = tf.summary
tfl = tf.keras.layers

import tensorflow_probability as tfp
tfd = tfp.distributions

import tensorflow_datasets as tfds

AVAILABLE_MODELS = [
    "gaussian",
    "mog",
    "snis"
]

logger = setup_logger(__name__, level=logging.DEBUG, to_console=False, log_file=f"../logs/snis_mnist.log")


class SNISNetwork(tfl.Layer):
    
    def __init__(self, hidden=100, activation="tanh", name="snis_network", **kwargs):
        
        super(SNISNetwork, self).__init__(name=name, **kwargs)
        
        self.hidden = hidden
        self.activation = activation
       
    def build(self, input_size): 
        
        self.layers = [
            tfl.Dense(units=self.hidden,
                      activation=self.activation),
            tfl.Dense(units=self.hidden,
                      activation=self.activation),
            tfl.Dense(units=1,
                      activation=None)
        ]
        
        super(SNISNetwork, self).build(input_size)
        
    def call(self, tensor):
          
        for layer in self.layers:
            
            tensor = layer(tensor)
            
        return tensor


def main(args):

    logger.info(f"Tensorflow was built with CUDA: {tf.test.is_built_with_cuda()}")
    logger.info(f"Tensorflow is using GPU: {tf.test.is_gpu_available()}")

    batch_size = 128
    log_freq = 1000
    anneal_end = 100000
    drop_learning_rate_after_iter = 1000000

    # Get dataset
    dataset = tfds.load("binarized_mnist",
                        data_dir=args.dataset)

    train_ds = dataset["train"]

    # Normalize data
    train_ds = train_ds.map(lambda x: tf.cast(x["image"], tf.float32))

    # Prepare the dataset for training
    train_ds = train_ds.shuffle(5000)
    train_ds = train_ds.repeat()
    train_ds = train_ds.batch(batch_size)
    train_ds = train_ds.prefetch(32)

    # Get model
    if args.model == "gaussian":
        model = MNISTVAE(name="gaussian_mnist_vae",
                         prior=tfd.Normal(loc=tf.zeros(50),
                                          scale=tf.ones(50)))

    elif args.model == "mog":

        num_components = 20

        logits = tf.Variable(tf.random.uniform(shape=(50, num_components), minval=-1., maxval=1.))
        loc = tf.Variable(tf.random.uniform(shape=(num_components, 50), minval=-1., maxval=1.))
        log_scale = tf.Variable(tf.random.uniform(shape=(num_components, 50), minval=-1., maxval=1.))

        scale = 1e-4 + tf.nn.softplus(log_scale)

        components = [tfd.Normal(loc=loc[i, :], scale=scale[i, :]) for i in range(num_components)]

        mixture = tfd.Mixture(cat=tfd.Categorical(logits=logits),
                              components=components)

        model = MNISTVAE(name="mog_mnist_vae",
                         prior=mixture)

    elif args.model == "snis":

        prior = SNISDistribution(energy_fn=SNISNetwork(hidden=100),
                                 prior=tfd.Normal(loc=tf.zeros(50),
                                                  scale=tf.ones(50)),
                                 K=1024)

        model = MNISTVAE(name="snis_mnist_vae",
                         prior=prior)

    # Get optimizer
    learn_rate = tf.Variable(3e-4)
    optimizer = tf.optimizers.Adam(learning_rate=learn_rate)

    # Get checkpoint
    ckpt = tf.train.Checkpoint(step=tf.Variable(1, dtype=tf.int64),
                               learn_rate=learn_rate,
                               model=model,
                               optimizer=optimizer)

    manager = tf.train.CheckpointManager(ckpt, args.save_dir, max_to_keep=3)

    # Get summary writer
    current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = args.save_dir + "/logs/" + current_time + "/train"

    summary_writer = tfs.create_file_writer(log_dir)

    # Initialize the model by passing zeros through it
    model(tf.zeros([1, 28, 28, 1]))

    # Restore previous session
    ckpt.restore(manager.latest_checkpoint)
    if manager.latest_checkpoint:
        logger.info(f"Restored model from {manager.latest_checkpoint}")
    else:
        logger.info("Initializing model from scratch.")

    for batch in train_ds.take(args.iters - int(ckpt.step)):

        # Increment the training step
        ckpt.step.assign_add(1)

        # Decrease learning rate after a while
        if int(ckpt.step) == drop_learning_rate_after_iter:
            learn_rate.assign(1e-5)

        with tf.GradientTape() as tape:

            reconstruction = model(batch, training=True)

            log_prob = model.likelihood.log_prob(batch)

            nll = -tf.reduce_mean(log_prob)
            kl_divergence = tf.reduce_mean(model.kl_divergence)

            beta = tf.minimum(1., tf.cast(ckpt.step / anneal_end, tf.float32))

            loss = nll + beta * kl_divergence

        if tf.reduce_min(-log_prob) > 10:
            logger.info("Mispredicted pixel!")

        # Check for NaN loss
        if tf.math.is_nan(loss):
            logger.error(f"Loss was NaN, stopping! nll: {nll}, KL: {kl_divergence} ")
            break

        gradients = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(gradients, model.trainable_variables))

        if int(ckpt.step) % log_freq == 0:
            save_path = manager.save()
            logger.info(f"Step {int(ckpt.step)}: Saved model to {save_path}")

            with summary_writer.as_default():
                tfs.scalar(name="Loss", data=loss, step=ckpt.step)
                tfs.scalar(name="NLL", data=nll, step=ckpt.step)
                tfs.scalar(name="KL", data=kl_divergence, step=ckpt.step)
                tfs.scalar(name="Beta", data=beta, step=ckpt.step)

                tfs.image(name="Original", data=batch, step=ckpt.step)
                tfs.image(name="Reconstruction", data=reconstruction, step=ckpt.step)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--dataset", "-D", type=str, required=True,
                        help="Path to the binarized MNIST dataset. "
                             "If does not exist, it will be downloaded to that location.")

    parser.add_argument("--model", "-M", choices=AVAILABLE_MODELS, required=True,
                        help="Select which model to train.")

    parser.add_argument("--save_dir", "-S", required=True,
                        help="Path for the model checkpoints.")

    parser.add_argument("--iters", type=int, default=10000000)

    args = parser.parse_args()

    main(args)
