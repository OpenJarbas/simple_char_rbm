import numpy as np
import enum
from char_rbm import utils

MAX_PROG_SAMPLE_INTERVAL = 10000

# If true, then subtract a constant between iterations when annealing, rather than dividing by a constant.
# Literature seems divided on the best way to do this? Anecdotally, seem to get better results with exp
# decay most of the time, but haven't looked very carefully.
LINEAR_ANNEAL = 0

BIG_NUMBER = 3.0


class shrink_model(object):
    def __init__(self, model, min_length, max_length):
        assert 1 <= max_length <= model.codec.maxlen
        assert 0 <= min_length <= model.codec.maxlen
        self.model = model
        self.min_length = min_length
        self.max_length = max_length

    def __enter__(self):
        codec = self.model.codec
        model = self.model
        padidx = codec.char_lookup[codec.filler]
        self.prev_biases = [
            model.intercept_visible_[codec.nchars * posn + padidx] for posn in
            range(codec.maxlen)]
        # Force padding character off for all indices up to min length
        for posn in range(self.min_length):
            model.intercept_visible_[
                codec.nchars * posn + padidx] += -1 * BIG_NUMBER

        # Force padding character *on* for indices past max length
        for posn in range(self.max_length, codec.maxlen):
            model.intercept_visible_[codec.nchars * posn + padidx] += BIG_NUMBER

    def __exit__(self, *args):
        padidx = self.model.codec.char_lookup[self.model.codec.filler]
        for posn, bias in enumerate(self.prev_biases):
            self.model.intercept_visible_[
                self.model.codec.nchars * posn + padidx] = bias


class VisInit(enum.Enum):
    """Ways of initializing visible units before repeated gibbs sampling."""
    # All zeros. Should be basically equivalent to deferring to the *hidden* biases.
    zeros = 1
    # Treat visible biases as softmax
    biases = 2
    # Turn on each unit (not just each one-hot vector) with p=.5
    uniform = 3
    spaces = 4
    padding = 7  # Old models use ' ' as filler, making this identical to the above
    # Training examples
    train = 5
    # Choose a random length. Fill in that many uniformly random chars. Fill the rest with padding character.
    chunks = 6
    # Use training examples but randomly mutate non-space/padding characters. Only the "shape" is preserved.
    silhouettes = 8
    # Valid one-hot vectors, each chosen uniformly at random
    uniform_chars = 9


class BadInitMethodException(Exception):
    pass


def starting_visible_configs(init_method, n, model,
                             training_examples_fname=None):
    """Return an ndarray of n visible configurations for the given model
    according to the specified init method (which should be a member of the VisInit enum)
    """
    vis_shape = (n, model.intercept_visible_.shape[0])
    maxlen, nchars = model.codec.maxlen, model.codec.nchars
    if init_method == VisInit.biases:
        sm = np.tile(model.intercept_visible_, [n, 1]).reshape(
            (-1,) + model.codec.shape())
        return utils.softmax_and_sample(sm).reshape(vis_shape)
    elif init_method == VisInit.zeros:
        return np.zeros(vis_shape)
    elif init_method == VisInit.uniform:
        return np.random.randint(0, 2, vis_shape)
    # This will fail if ' ' isn't in the alphabet of this model
    elif init_method == VisInit.spaces or init_method == VisInit.padding:
        fillchar = {VisInit.spaces: ' ', VisInit.padding: model.codec.filler}[
            init_method]
        vis = np.zeros((n,) + model.codec.shape())
        try:
            fill = model.codec.char_lookup[fillchar]
        except KeyError:
            raise BadInitMethodException(fillchar + " is not in model alphabet")

        vis[:, :, fill] = 1
        return vis.reshape(vis_shape)
    elif init_method == VisInit.train or init_method == VisInit.silhouettes:
        assert training_examples_fname is not None, "No training examples provided to initialize with"
        mutagen = model.codec.mutagen_silhouettes if init_method == VisInit.silhouettes else None
        examples = utils.vectors_from_txtfile(training_examples_fname,
                                              model.codec, limit=n,
                                              mutagen=mutagen)
        return examples
    elif init_method == VisInit.chunks or init_method == VisInit.uniform_chars:
        # This works, but probably isn't idiomatic numpy.
        # I don't think I'll ever write idiomatic numpy.

        # Start w uniform dist
        char_indices = np.random.randint(0, nchars, (n, maxlen))
        if init_method == VisInit.chunks:
            # Choose some random lengths
            lengths = np.clip(
                maxlen * .25 * np.random.randn(n) + (maxlen * .66), 1, maxlen
                ).astype('int8').reshape(n, 1)
            _, i = np.indices((n, maxlen))
            char_indices[i >= lengths] = model.codec.char_lookup[
                model.codec.filler]

        # TODO: This is a useful little trick. Make it a helper function and reuse it elsewhere?
        return np.eye(nchars)[char_indices.ravel()].reshape(vis_shape)
    else:
        raise ValueError("Unrecognized init method: {}".format(init_method))


def print_sample_callback(sample_strings, i, energy=None, logger=None):
    if energy is None:
        text = "".join('{} \t {:.2f}'.format(t[0], t[1]) for t in
                        zip(sample_strings, energy))
    else:
        text = "".join(sample_strings)
    if logger is None:
        pass
        #print "\n" + text
    else:
        logger.debug(text)
    return text


@utils.timeit
def sample_model(model, n, iters, sample_iter_indices,
                 start_temp=1.0, final_temp=1.0,
                 callback=print_sample_callback, init_method=VisInit.biases,
                 training_examples=None,
                 sample_energy=False, starting_vis=None, min_length=0,
                 max_length=0,
                 ):
    if callback is None:
        callback = lambda: None
    if starting_vis is not None:
        vis = starting_vis
    else:
        vis = starting_visible_configs(init_method, n, model, training_examples)

    args = [model, vis, iters, sample_iter_indices, start_temp, final_temp,
            callback, sample_energy]
    if min_length or max_length:
        if max_length == 0:
            max_length = model.codec.maxlen
        with shrink_model(model, min_length, max_length):
            return _sample_model(*args)
    else:
        return _sample_model(*args)


def _sample_model(model, vis, iters, sample_iter_indices, start_temp,
                  final_temp, callback,
                  sample_energy):
    temp = start_temp
    temp_decay = (final_temp / start_temp) ** (1 / iters)
    temp_delta = (final_temp - start_temp) / iters
    next_sample_metaindex = 0
    samples = []
    for i in range(iters):
        if i == sample_iter_indices[next_sample_metaindex]:
            # Time to take samples
            sample_strings = [model.codec.decode(v, pretty=True, strict=False)
                              for v in vis]
            if sample_energy:
                energy = model._free_energy(vis)
                sample = callback(sample_strings, i, energy)
            else:
                sample = callback(sample_strings, i)
            samples.append(sample_strings)
            next_sample_metaindex += 1
            if next_sample_metaindex == len(sample_iter_indices):
                break
        vis = model.gibbs(vis, temp)
        if LINEAR_ANNEAL:
            temp += temp_delta
        else:
            temp *= temp_decay
    return vis, samples[-1]
