from sacred import Experiment
from sacred.stflow import LogFileWriter

import json
import datetime

import tensorflow as tf
import tensorflow_probability as tfp

from rec.models.mnist_vae import MNISTVAE
from rec.models.resnet_vae import BidirectionalResNetVAE

from datasets import data_ingredient, load_dataset

tfs = tf.summary
tfd = tfp.distributions

ex = Experiment('train_generative_model', ingredients=[data_ingredient])


@ex.config
def default_config(dataset_info):

    # Model configurations
    model_save_base_dir = "/scratch/gf332/models/relative-entropy-coding"

    model = "vae"

    if model == "vae":
        model_config = {
            "latent_size": 50
        }

        learning_rate = 3e-4
        lamb = 0.
        beta = 1.

    elif model == "resnet_vae":
        model_config = {
            "latent_size": "variable",
            "num_res_blocks": 20,
            "deterministic_filters": 160,
            "stochastic_filters": 32,
        }

        learning_rate = 1e-2
        lamb = 0.25
        beta = 1.

    # Training-time configurations
    iters = 3000000

    shuffle_buffer_size = 5000
    batch_size = 64
    num_prefetch = 32

    # ELBO related stuff
    beta = 1.
    annealing_end = 100000  # Steps after which beta is fixed
    drop_learning_rate_after_iter = 1500000
    learning_rate_after_drop = 1e-5

    # Logging
    tensorboard_log_freq = 1000

    model_save_dir = f"{model_save_base_dir}/{dataset_info['tfds_name']}/{model}/" \
                     f"latents_{model_config['latent_size']}_beta_{beta:.3f}_lamb_{lamb:.3f}"

    current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = f"{model_save_dir}/logs/{current_time}/train"


@ex.capture
@LogFileWriter(ex)
def train_vae(dataset,
              model_save_dir,
              log_dir,
              tensorboard_log_freq,
              model_config,
              batch_size,
              shuffle_buffer_size,
              num_prefetch,
              learning_rate,
              iters,
              beta,
              annealing_end,
              drop_learning_rate_after_iter,
              learning_rate_after_drop,
              _log):
    # -------------------------------------------------------------------------
    # Prepare the dataset
    # -------------------------------------------------------------------------
    dataset = dataset.shuffle(shuffle_buffer_size)
    dataset = dataset.repeat()
    dataset = dataset.batch(batch_size)
    dataset = dataset.prefetch(num_prefetch)

    # -------------------------------------------------------------------------
    # Create model
    # -------------------------------------------------------------------------
    latent_size = model_config["latent_size"]
    model = MNISTVAE(prior=tfd.Normal(loc=tf.zeros(latent_size),
                                      scale=tf.ones(latent_size)))

    # -------------------------------------------------------------------------
    # Create Optimizer
    # -------------------------------------------------------------------------
    learn_rate = tf.Variable(learning_rate)
    optimizer = tf.optimizers.Adam(learn_rate)

    # -------------------------------------------------------------------------
    # Create Checkpoints
    # -------------------------------------------------------------------------
    ckpt = tf.train.Checkpoint(step=tf.Variable(1, dtype=tf.int64),
                               learn_rate=learn_rate,
                               model=model,
                               optimizer=optimizer)

    manager = tf.train.CheckpointManager(ckpt, model_save_dir, max_to_keep=3)

    # -------------------------------------------------------------------------
    # Create Summary Writer
    # -------------------------------------------------------------------------
    summary_writer = tfs.create_file_writer(log_dir)

    # -------------------------------------------------------------------------
    # Training Loop
    # -------------------------------------------------------------------------

    # Initialize the model weights
    model(tf.zeros([1, 28, 28, 1]))

    # Restore previous session
    ckpt.restore(manager.latest_checkpoint)
    if manager.latest_checkpoint:
        _log.info(f"Restored model from {manager.latest_checkpoint}")
    else:
        _log.info("Initializing model from scratch.")

    for batch in dataset.take(iters - int(ckpt.step)):

        # Increment the training step
        ckpt.step.assign_add(1)

        # Decrease learning rate after a while
        if int(ckpt.step) == drop_learning_rate_after_iter:
            learn_rate.assign(learning_rate_after_drop)

        with tf.GradientTape() as tape:

            reconstruction = model(batch, training=True)

            log_prob = model.likelihood.log_prob(batch)

            # Get the empirical log-likelihood per image
            nll = -tf.reduce_mean(tf.reduce_sum(log_prob, axis=[1, 2]))

            # Get the empirical KL per latent code
            kl_divergence = tf.reduce_mean(tf.reduce_sum(model.kl_divergence, axis=1))

            # Linearly annealed beta
            beta = tf.minimum(beta, tf.cast(ckpt.step / annealing_end, tf.float32))

            loss = nll + beta * kl_divergence

        if tf.reduce_min(-log_prob) > 10:
            _log.info("Mispredicted pixel!")

        gradients = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(gradients, model.trainable_variables))

        if int(ckpt.step) % tensorboard_log_freq == 0:
            # Get empirical posterior and prior log likelihoods for summaries
            post_log_lik = tf.reduce_mean(tf.reduce_sum(model.post_log_liks, axis=1))
            prior_log_lik = tf.reduce_mean(tf.reduce_sum(model.prior_log_liks, axis=1))

            expected_max_kl = tf.reduce_mean(tf.reduce_max(model.kl_divergence, axis=1))

            # Save model
            save_path = manager.save()
            _log.info(f"Step {int(ckpt.step)}: Saved model to {save_path}")

            with summary_writer.as_default():
                tfs.scalar(name="Loss", data=loss, step=ckpt.step)
                tfs.scalar(name="NLL", data=nll, step=ckpt.step)
                tfs.scalar(name="Posterior_LL", data=post_log_lik, step=ckpt.step)
                tfs.scalar(name="Prior_LL", data=prior_log_lik, step=ckpt.step)
                tfs.scalar(name="KL", data=kl_divergence, step=ckpt.step)

                tfs.scalar(name="Beta", data=beta, step=ckpt.step)

                tfs.image(name="Original", data=batch, step=ckpt.step)
                tfs.image(name="Reconstruction", data=reconstruction, step=ckpt.step)

                tfs.scalar(name="Expected_Max_KL", data=expected_max_kl, step=ckpt.step)


@ex.capture
def train_resnet_vae(dataset,
                     model_save_dir,
                     log_dir,
                     tensorboard_log_freq,
                     model_config,
                     batch_size,
                     shuffle_buffer_size,
                     num_prefetch,
                     learning_rate,
                     iters,
                     beta,
                     lamb,
                     drop_learning_rate_after_iter,
                     learning_rate_after_drop,
                     num_pixels,
                     num_channels,
                     _log):
    # -------------------------------------------------------------------------
    # Prepare the dataset
    # -------------------------------------------------------------------------
    dataset = dataset.shuffle(shuffle_buffer_size)
    dataset = dataset.repeat()
    dataset = dataset.batch(batch_size)
    dataset = dataset.prefetch(num_prefetch)

    # -------------------------------------------------------------------------
    # Create model
    # -------------------------------------------------------------------------
    model = BidirectionalResNetVAE(**model_config)

    # -------------------------------------------------------------------------
    # Create Optimizer
    # -------------------------------------------------------------------------
    learn_rate = tf.Variable(learning_rate)
    optimizer = tf.optimizers.Adamax(learn_rate)

    # -------------------------------------------------------------------------
    # Create Checkpoints
    # -------------------------------------------------------------------------
    ckpt = tf.train.Checkpoint(step=tf.Variable(1, dtype=tf.int64),
                               learn_rate=learn_rate,
                               model=model,
                               optimizer=optimizer)

    manager = tf.train.CheckpointManager(ckpt, model_save_dir, max_to_keep=3)

    # -------------------------------------------------------------------------
    # Create Summary Writer
    # -------------------------------------------------------------------------
    summary_writer = tfs.create_file_writer(log_dir)

    # -------------------------------------------------------------------------
    # Training Loop
    # -------------------------------------------------------------------------

    # Initialize the model weights
    for first_pass in dataset.take(1):
        model(first_pass)

    # Restore previous session
    ckpt.restore(manager.latest_checkpoint)
    if manager.latest_checkpoint:
        _log.info(f"Restored model from {manager.latest_checkpoint}")
    else:
        _log.info("Initializing model from scratch.")

    def elbo(log_probs, kls, beta, lamb):
        ll = tf.reduce_sum(tf.reduce_mean(log_probs, axis=0))
        mean_kls = [tf.reduce_sum(tf.reduce_mean(kl, axis=0)) for kl in kls]

        mean_max_kls = [tf.maximum(lamb, kl) for kl in mean_kls]

        total_mean_max_kl = tf.reduce_sum(mean_max_kls)

        _elbo = ll - beta * total_mean_max_kl

        return _elbo, ll, mean_max_kls

    for batch in dataset.take(iters - int(ckpt.step)):

        # Increment the training step
        ckpt.step.assign_add(1)

        # Decrease learning rate after a while
        if int(ckpt.step) == drop_learning_rate_after_iter:
            learn_rate.assign(learning_rate_after_drop)

        with tf.GradientTape() as tape:

            reconstruction = model(batch, training=True)

            log_likelihood = model.log_likelihood
            kls = model.kl_divergence(empirical=False)

            # Linearly annealed beta
            # beta = tf.minimum(beta, tf.cast(ckpt.step / annealing_end, tf.float32))

            loss, ll, kl = elbo(log_likelihood, kls, beta=beta, lamb=lamb)
            loss = -loss

            total_kl = tf.reduce_sum(kl)

        if tf.math.is_nan(loss) or tf.math.is_inf(loss):
            raise Exception(f"Loss blew up: {loss:.3f}, NLL: {-ll:.3f}, KL: {total_kl:.3f}")

        gradients = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(gradients, model.trainable_variables))

        if int(ckpt.step) % tensorboard_log_freq == 0:
            # Save model
            save_path = manager.save()
            _log.info(f"Step {int(ckpt.step)}: Saved model to {save_path}")

            log_2 = tf.math.log(2.)

            true_elbo, _, _ = elbo(log_likelihood, kls, beta=1., lamb=0.)
            _, _, true_kl = elbo(log_likelihood, kls, beta=beta, lamb=0.)
            true_total_kl = tf.reduce_sum(true_kl)

            with summary_writer.as_default():
                tfs.scalar(name="Loss", data=loss, step=ckpt.step)
                tfs.scalar(name="NLL", data=-ll, step=ckpt.step)
                tfs.scalar(name="KL", data=total_kl, step=ckpt.step)
                tfs.scalar(name="Lossy_Bits_per_pixel",
                           data=true_total_kl / (num_pixels * log_2),
                           step=ckpt.step)
                tfs.scalar(name="Lossy_Bits_per_pixel_and_channel",
                           data=true_total_kl / (num_pixels * num_channels * log_2),
                           step=ckpt.step)
                tfs.scalar(name="Lossless_Bits_per_pixel",
                           data=-true_elbo / (num_pixels * log_2),
                           step=ckpt.step)
                tfs.scalar(name="Lossless_Bits_per_pixel_and_channel",
                           data=-true_elbo / (num_pixels * num_channels * log_2),
                           step=ckpt.step)
                tfs.scalar(name="Likelihood_Scale",
                           data=tf.math.exp(model.likelihood_log_scale),
                           step=ckpt.step)

                tfs.image(name="Original", data=batch, step=ckpt.step)
                tfs.image(name="Reconstruction", data=reconstruction, step=ckpt.step)

                for i in range(len(kls)):
                    tfs.scalar(name=f"KL/dim_{i + 1}", data=tf.squeeze(kl[i]), step=ckpt.step)

@ex.automain
def train_model(model, _log):
    dataset, num_pixels, num_channels = load_dataset()

    if model == "vae":
        _log.info("Training a regular VAE!")
        train_vae(dataset=dataset)

    elif model == "resnet_vae":
        _log.info("Training a ResNet VAE!")
        train_resnet_vae(dataset=dataset,
                         num_pixels=num_pixels,
                         num_channels=num_channels)
