from functools import partial  # for use with vmap
import jax
import jax.numpy as jnp
import haiku as hk
import jax.scipy.stats.norm as norm
import optax
from jax.experimental import optimizers
from dataclasses import dataclass


@dataclass
class EnsembleBlockConfig:
    shape: tuple = (2,)
    model_number: int = 5


class EnsembleBlock(hk.Module):
    def __init__(self, config: EnsembleBlockConfig = None, name=None):
        super().__init__(name=name)
        if config is None:
            config = EnsembleBlockConfig()
        self.config = config

    # x is of shape ([ensemble_num, *seqs.shape])
    def __call__(self, x):
        out = jnp.array([hk.nets.MLP(self.config.shape)(x[i])
                         for i in range(self.config.model_number)])
        return out


def model_forward(x):
    e = EnsembleBlock()
    return e(x)


def model_reduce(out):
    mu = jnp.mean(out[..., 0], axis=0)
    std = jnp.mean(out[..., 1] + out[..., 0]**2, axis=0) - mu**2
    return mu, std


def _deep_ensemble_loss(forward, params, seqs, labels):
    out = forward.apply(params, seqs)
    means = out[..., 0]
    stds = out[..., 1]
    n_log_likelihoods = 0.5 * \
        jnp.log(jnp.abs(stds)) + 0.5*(labels-means)**2/jnp.abs(stds)
    return jnp.sum(n_log_likelihoods, axis=0)


def _adv_loss_func(forward, params, seqs, labels):
    epsilon = 1e-5
    grad_inputs = jax.grad(_deep_ensemble_loss, 2)(
        forward, params, seqs, labels)
    seqs_ = seqs + epsilon * jnp.sign(grad_inputs)
    return _deep_ensemble_loss(forward, params, seqs, labels) + _deep_ensemble_loss(forward, params, seqs_, labels)


def shuffle_in_unison(key, a, b):
    assert len(a) == len(b)
    p = jax.random.permutation(key, len(a))
    return jnp.array([a[i] for i in p]), jnp.array([b[i] for i in p])

def ensemble_train(key, forward, seqs, labels, val_seqs, val_labels):
    learning_rate = 1e-2
    n_step = 3

    opt_init, opt_update = optax.chain(
        optax.scale_by_adam(b1=0.8, b2=0.9, eps=1e-4),
        optax.scale(-learning_rate)  # minus sign -- minimizing the loss
    )

    key, key_ = jax.random.split(key, num=2)
    params = forward.init(key, seqs)
    opt_state = opt_init(params)

    @jax.jit
    def train_step(opt_state, params, seq, label):
        seq_tile = jnp.tile(seq, (5, 1))
        label_tile = jnp.tile(label, 5)
        grad = jax.grad(_adv_loss_func, 1)(
            forward, params, seq_tile, label_tile)
        updates, opt_state = opt_update(grad, opt_state, params)
        params = optax.apply_updates(params, updates)
        loss = _adv_loss_func(forward, params, seq_tile, label_tile)
        return opt_state, params, loss
    losses = []
    val_losses = []
    for i in range(n_step):
        print(i)
        batch_loss = 0. # average loss over each training step
        # shuffle seqs and labels
        key, key_ = jax.random.split(key, num=2)
        shuffle_seqs, shuffle_labels = shuffle_in_unison(key, seqs, labels)
        for i in range(len(shuffle_labels)):
            seq = shuffle_seqs[i]
            label = shuffle_labels[i]
            opt_state, params, loss = train_step(opt_state, params, seq, label)
            #compute validation loss
            val_loss = 0.
            for j in range(len(val_labels)):
                val_seq = val_seqs[j]
                val_label = val_labels[j]
                val_seq_tile = jnp.tile(val_seq, (5, 1))
                val_label_tile = jnp.tile(val_label, 5)
                val_loss += _adv_loss_func(forward, params, val_seq_tile, val_label_tile)
            val_loss = val_loss/len(val_labels)
            #batch_loss += loss
            losses.append(loss)
            val_losses.append(val_loss)
        #losses.append(batch_loss/len(shuffle_labels))
    #outs = forward.apply(params, seqs)
    #joint_outs = model_reduce(outs)
    return params, losses, val_losses


def bayesian_ei(f, params, init_x, Y):
    out = f.apply(params, init_x)
    joint_out = model_reduce(out)
    mu = joint_out[0]
    std = joint_out[1]
    #mus = f.apply(params, X)[...,0]
    best = jnp.max(Y)
    epsilon = 0.1
    z = (mu-best-epsilon)/std
    return -(mu-best-epsilon)*norm.cdf(z) - std*norm.pdf(z)


def bayes_opt(f, params, labels):
    key = jax.random.PRNGKey(0)
    key, _ = jax.random.split(key, num=2)
    eta = 1e-2
    n_steps = 50
    init_x = jax.random.normal(key, shape=(1, 1900))
    opt_init, opt_update, get_params = optimizers.adam(
        step_size=eta, b1=0.8, b2=0.9, eps=1e-5)
    opt_state = opt_init(init_x)

    @jax.jit
    def step(i, opt_state):
        x = get_params(opt_state)
        loss, g = jax.value_and_grad(bayesian_ei, 2)(
            f, params, x, labels)
        return opt_update(i, g, opt_state), loss

    for step_idx in range(n_steps):
        opt_state, loss = step(step_idx, opt_state)

    final_vec = get_params(opt_state)
    return final_vec
