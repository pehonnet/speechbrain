"""
Losses for training neural networks.

Authors
 * Mirco Ravanelli 2020
"""

import torch
from torch import nn
import logging
import functools
from speechbrain.data_io.data_io import length_to_mask
from itertools import permutations


logger = logging.getLogger(__name__)


def transducer_loss(log_probs, targets, input_lens, target_lens, blank_index):
    """Transducer loss, see `speechbrain/nnet/transducer/transducer_loss.py`

    Arguments
    ---------
    predictions : torch.Tensor
        Predicted tensor, of shape [batch, time, chars].
    targets : torch.Tensor
        Target tensor, without any blanks, of shape [batch, target_len]
    input_lens : torch.Tensor
        Length of each utterance.
    target_lens : torch.Tensor
        Length of each target sequence.
    blank_index : int
        The location of the blank symbol among the character indexes.
    """
    from speechbrain.nnet.transducer.transducer_loss import Transducer

    input_lens = (input_lens * log_probs.shape[1]).int()
    target_lens = (target_lens * targets.shape[1]).int()
    return Transducer.apply(
        log_probs,
        targets,
        input_lens,
        target_lens,
        blank_index,
        reduction="mean",
    )


class PitWrapper(nn.Module):
    """
    Permutation Invariant Wrapper module to allow Permutation Invariant Training (PIT) with existing losses.
    Permutation invariance is calculated over sources/classes axis which is assumed to be the rightmost dimension: tensors
    are assumed thus to have shape [batch, ..., channels, sources].

    Arguments
    ---------
    base_loss : function
        base loss function, e.g. torch.nn.MSELoss. It is assumed that it takes two arguments: predictions and targets and no reduction is performed.
        (if a pytorch loss is used, the user must specify reduction="none").
    Example
    -------
    pit_mse = PitWrapper(nn.MSELoss(reduction="none"))
    targets = torch.rand((2, 32, 4))
    p = (3, 0, 2, 1)
    predictions = targets[..., p]
    loss, opt_p = pit(predictions, targets) # loss should be zero
    """

    def __init__(self, base_loss):
        super(PitWrapper, self).__init__()
        self.base_loss = base_loss

    def _fast_pit(self, loss_mat):

        loss = None
        assigned_perm = None
        for p in permutations(range(loss_mat.shape[0])):
            c_loss = loss_mat[range(loss_mat.shape[0]), p].mean()
            if loss is None or loss > c_loss:
                loss = c_loss
                assigned_perm = p
        return loss, assigned_perm

    def _opt_perm_loss(self, pred, target):

        n_sources = pred.size(-1)
        pred = pred.unsqueeze(-2).repeat(*[1 for x in range(len(pred.shape)-1)], n_sources, 1)
        target = target.unsqueeze(-1).repeat(1, *[1 for x in range(len(target.shape)-1)], n_sources)

        loss_mat = self.base_loss(pred, target)
        assert len(loss_mat.shape) >= 2, "Base loss should not perform any reduction operation"
        mean_over = [x for x in range(len(loss_mat.shape))]
        loss_mat = loss_mat.mean(dim=mean_over[:-2])

        return self._fast_pit(loss_mat)

    def reorder_tensor(self, tensor, p):
        """
            Arguments
            ---------
            tensor : torch.Tensor
                tensor to reorder given the optimal permutation, of shape [batch, channels, ..., sources].
            p : list of tuples
                list of optimal permutations, e.g. for batch=2 and n_sources=3 [(0, 1, 2), (0, 2, 1].
            """

        new = torch.zeros_like(tensor).to(tensor.device)
        for b in range(tensor.shape[0]):
            new[b] = tensor[b][..., p[b]].clone()
        return new

    def forward(self, preds, targets):
        """
            Arguments
            ---------
            preds : torch.Tensor
                Network predictions tensor, of shape [batch, channels, ..., sources].
            targets : torch.Tensor
                Target tensor, of shape [batch, channels, ..., sources].
            """
        losses = []
        perms = []
        for pred, label in zip(preds, targets):
            loss, p = self._opt_perm_loss(pred, label)
            perms.append(p)
            losses.append(loss)
        loss = torch.stack(losses)
        return loss, perms


def ctc_loss(log_probs, targets, input_lens, target_lens, blank_index):
    """CTC loss

    Arguments
    ---------
    predictions : torch.Tensor
        Predicted tensor, of shape [batch, time, chars].
    targets : torch.Tensor
        Target tensor, without any blanks, of shape [batch, target_len]
    input_lens : torch.Tensor
        Length of each utterance.
    target_lens : torch.Tensor
        Length of each target sequence.
    blank_index : int
        The location of the blank symbol among the character indexes.
    """
    input_lens = (input_lens * log_probs.shape[1]).int()
    target_lens = (target_lens * targets.shape[1]).int()
    log_probs = log_probs.transpose(0, 1)
    return torch.nn.functional.ctc_loss(
        log_probs,
        targets,
        input_lens,
        target_lens,
        blank_index,
        zero_infinity=True,
    )


def l1_loss(predictions, targets, length=None, allowed_len_diff=3):
    """Compute the true l1 loss, accounting for length differences.

    Arguments
    ---------
    predictions : torch.Tensor
        Predicted tensor, of shape ``[batch, time, *]``.
    targets : torch.Tensor
        Target tensor, same size as predicted tensor.
    length : torch.Tensor
        Length of each utterance for computing true error with a mask.
    allowed_len_diff : int
        Length difference that will be tolerated before raising an exception.

    Example
    -------
    >>> probs = torch.tensor([[0.9, 0.1, 0.1, 0.9]])
    >>> l1_loss(probs, torch.tensor([[1., 0., 0., 1.]]))
    tensor(0.1000)
    """
    predictions, targets = truncate(predictions, targets, allowed_len_diff)
    loss = functools.partial(torch.nn.functional.l1_loss, reduction="none")
    return compute_masked_loss(loss, predictions, targets, length)


def mse_loss(predictions, targets, length=None, allowed_len_diff=3):
    """Compute the true mean squared error, accounting for length differences.

    Arguments
    ---------
    predictions : torch.Tensor
        Predicted tensor, of shape ``[batch, time, *]``.
    targets : torch.Tensor
        Target tensor, same size as predicted tensor.
    length : torch.Tensor
        Length of each utterance for computing true error with a mask.
    allowed_len_diff : int
        Length difference that will be tolerated before raising an exception.

    Example
    -------
    >>> probs = torch.tensor([[0.9, 0.1, 0.1, 0.9]])
    >>> mse_loss(probs, torch.tensor([[1., 0., 0., 1.]]))
    tensor(0.0100)
    """
    predictions, targets = truncate(predictions, targets, allowed_len_diff)
    loss = functools.partial(torch.nn.functional.mse_loss, reduction="none")
    return compute_masked_loss(loss, predictions, targets, length)


def classification_error(
    probabilities, targets, length=None, allowed_len_diff=3
):
    """Computes the classification error at frame or batch level.

    Arguments
    ---------
    probabilities : torch.Tensor
        The posterior probabilities of shape
        [batch, prob] or [batch, frames, prob]
    targets : torch.Tensor
        The targets, of shape [batch] or [batch, frames]
    length : torch.Tensor
        Length of each utterance, if frame-level loss is desired.
    allowed_len_diff : int
        Length difference that will be tolerated before raising an exception.

    Example
    -------
    >>> probs = torch.tensor([[[0.9, 0.1], [0.1, 0.9]]])
    >>> classification_error(probs, torch.tensor([1, 1]))
    tensor(0.5000)
    """
    if len(probabilities.shape) == 3 and len(targets.shape) == 2:
        probabilities, targets = truncate(
            probabilities, targets, allowed_len_diff
        )

    def error(predictions, targets):
        predictions = torch.argmax(probabilities, dim=-1)
        return (predictions != targets).float()

    return compute_masked_loss(error, probabilities, targets.long(), length)


def nll_loss(
    log_probabilities,
    targets,
    length=None,
    label_smoothing=0.0,
    allowed_len_diff=3,
):
    """Computes negative log likelihood loss.

    Arguments
    ---------
    log_probabilities : torch.Tensor
        The probabilities after log has been applied.
        Format is [batch, log_p] or [batch, frames, log_p]
    targets : torch.Tensor
        The targets, of shape [batch] or [batch, frames]
    length : torch.Tensor
        Length of each utterance, if frame-level loss is desired.
    allowed_len_diff : int
        Length difference that will be tolerated before raising an exception.

    Example
    -------
    >>> probs = torch.tensor([[0.9, 0.1], [0.1, 0.9]])
    >>> nll_loss(torch.log(probs), torch.tensor([1, 1]))
    tensor(1.2040)
    """
    if len(log_probabilities.shape) == 3:
        log_probabilities, targets = truncate(
            log_probabilities, targets, allowed_len_diff
        )
        log_probabilities = log_probabilities.transpose(1, -1)

    # Pass the loss function but apply reduction="none" first
    loss = functools.partial(torch.nn.functional.nll_loss, reduction="none")
    return compute_masked_loss(
        loss,
        log_probabilities,
        targets.long(),
        length,
        label_smoothing=label_smoothing,
    )



def truncate(predictions, targets, allowed_len_diff=3):
    """Ensure that predictions and targets are the same length.

    Arguments
    ---------
    predictions : torch.Tensor
        First tensor for checking length.
    targets : torch.Tensor
        Second tensor for checking length.
    allowed_len_diff : int
        Length difference that will be tolerated before raising an exception.
    """
    len_diff = predictions.shape[1] - targets.shape[1]
    if len_diff == 0:
        return predictions, targets
    elif abs(len_diff) > allowed_len_diff:
        raise ValueError(
            "Predictions and targets should be same length, but got %s and "
            "%s respectively." % (predictions.shape[1], targets.shape[1])
        )
    elif len_diff < 0:
        return predictions, targets[:, : predictions.shape[1]]
    else:
        return predictions[:, : targets.shape[1]], targets


def compute_masked_loss(
    loss_fn, predictions, targets, length=None, label_smoothing=0.0
):
    """Compute the true average loss of a set of waveforms of unequal length.

    Arguments
    ---------
    loss_fn : function
        A function for computing the loss taking just predictions and targets.
        Should return all the losses, not a reduction (e.g. reduction="none")
    predictions : torch.Tensor
        First argument to loss function.
    targets : torch.Tensor
        Second argument to loss function.
    length : torch.Tensor
        Length of each utterance to compute mask. If None, global average is
        computed and returned.
    label_smoothing: float
        The proportion of label smoothing. Should only be used for NLL loss.
        Ref: Regularizing Neural Networks by Penalizing Confident Output Distributions.
        https://arxiv.org/abs/1701.06548
    """
    mask = torch.ones_like(targets)
    if length is not None:
        mask = length_to_mask(
            length * targets.shape[1], max_len=targets.shape[1],
        )
        if len(targets.shape) == 3:
            mask = mask.unsqueeze(2).repeat(1, 1, targets.shape[2])

    loss = torch.sum(loss_fn(predictions, targets) * mask) / torch.sum(mask)
    if label_smoothing == 0:
        return loss
    else:
        loss_reg = -torch.sum(
            torch.mean(predictions, dim=1) * mask
        ) / torch.sum(mask)
        return label_smoothing * loss_reg + (1 - label_smoothing) * loss
