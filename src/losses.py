from typing import Dict, Optional, Protocol, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F


_EPSILON = 1e-7


def apply_reduction(input: torch.Tensor, reduction: str, dim=0):
    if reduction == "none":
        return input

    if input.shape[0] == 0:
        input = torch.Tensor([0]).type(dtype=input.dtype).to(device=input.device)
    if reduction == "mean":
        return torch.mean(input, dim=dim)
    if reduction == "sum":
        return torch.sum(input, dim=dim)

    raise ValueError(
        "Invalid reduction. Supported values are 'none', 'mean' and 'sum'."
    )


def categorical_focal_loss_with_logits(
    input: torch.Tensor,
    target: torch.Tensor,
    weight: Optional[torch.Tensor] = None,
    alpha=1.0,
    gamma=2.0,
    reduction: str = "mean",
):
    ce_loss = F.cross_entropy(input, target, reduction="none")
    pt = torch.exp(-ce_loss)
    loss = alpha * (1 - pt) ** gamma * ce_loss

    if weight is not None:
        weight = weight[target.long()]
        loss *= weight

    return apply_reduction(loss, reduction=reduction)


class Loss(Protocol):
    def __call__(self, outputs, targets: Dict[str, torch.Tensor]) -> torch.FloatTensor:
        pass


class ComputeLoss(Protocol):
    def __call__(
        outputs, targets: Dict[str, torch.Tensor]
    ) -> Tuple[torch.FloatTensor, Dict[str, float]]:
        pass


def generative_loss(
    logits: torch.FloatTensor,
    labels: torch.IntTensor,
    reduction: str = "mean",
    mask: torch.Tensor = None,
) -> torch.FloatTensor:
    if mask is not None:
        logits = logits[mask.bool()]
        labels = labels[mask.bool()]

    # swap seq_length with vocabulary dimension
    logits = torch.transpose(logits, 1, 2)  # batch_size x seq_length x vocab
    loss = F.cross_entropy(
        input=logits, target=labels, reduction="none"
    )  # batch_size x seq_length
    n_tokens_per_sample = torch.sum(labels != -100, dim=-1)  # batch_size
    n_tokens_per_sample = torch.clamp(n_tokens_per_sample, min=_EPSILON)
    loss = torch.sum(loss, dim=-1) / n_tokens_per_sample  # batch_size
    loss = apply_reduction(loss, reduction=reduction)
    return loss


class EncoderDecoderGenerativeLoss(Loss):
    def __init__(self, reduction: str = "mean") -> None:
        self.reduction = reduction

    def __call__(
        self,
        outputs,
        targets: Dict[str, torch.Tensor],
        mask: torch.Tensor = None,
    ) -> torch.FloatTensor:
        logits = outputs["logits"]
        labels = targets["labels"]

        return generative_loss(logits, labels, reduction=self.reduction, mask=mask)


def rationale_loss(
    logits: torch.FloatTensor,
    labels: torch.IntTensor,
    passage_mask: torch.IntTensor,
    max_rationale_length: int,
    reduction="mean",
    mask: torch.Tensor = None,
) -> torch.FloatTensor:
    """
    li = w * BCE(y_pred_i, y_true_i)
    , where w = w_positive if y_true_i is positive
            w = w_negative if y_true_i is negative
    w_positive = totals / positives
    w_negative = totals / negatives
    , where totals, positives and negatives are computed for each sequence

    Ls = sum_i=1..seq_length li / sum(w_i)
    L = sum_s=1..N Ls / N,
    , where N is the #sequences whose rationale length is <= max_rationale_length
    """

    # rationale_logits = outputs[self.rationale_logits_name]
    # rationale_labels = targets[self.rationale_labels_name]
    # passage_mask = targets[self.passage_mask_name]

    labels = labels * passage_mask

    rationale_lengths = torch.sum(labels, dim=-1)  # batch_size
    valid_rationales = rationale_lengths <= max_rationale_length
    if mask is not None:
        valid_rationales = valid_rationales & mask.bool()

    labels = labels[valid_rationales]
    passage_mask = passage_mask[valid_rationales]
    logits = logits[valid_rationales]

    # n_sequences = torch.sum(valid_rationales)

    totals = torch.sum(passage_mask, -1, keepdim=True)  # N x 1
    positives = torch.sum(labels, -1, keepdim=True)  # N x 1
    negatives = totals - positives  # N x 1
    totals = torch.clamp(totals, min=_EPSILON).float()
    weights = torch.where(
        labels == 1.0, totals / positives, totals / negatives
    )  # N x seq_length
    weights = torch.where(weights != torch.inf, weights, 0.0)  # N x seq_length
    weights = weights * passage_mask  # N x seq_length
    normalize_factor = torch.clamp(
        torch.sum(weights, dim=-1, keepdim=True), min=_EPSILON
    )
    weights = weights / normalize_factor  # N x seq_length
    # weights = weights * valid_rationales / n_sequences

    # N x seq_length
    per_token_loss = F.binary_cross_entropy_with_logits(
        input=logits,
        target=labels,
        weight=weights,
        reduction="none",
    )

    loss = torch.sum(per_token_loss, dim=-1)  # N
    return apply_reduction(loss, reduction=reduction)


class EncoderDecoderRationaleLoss(Loss):
    def __init__(self, max_rationale_length: int, reduction: str = "mean") -> None:
        self.max_rationale_length = max_rationale_length
        self.reduction = reduction

    def __call__(
        self,
        outputs,
        targets: Dict[str, torch.Tensor],
        mask: torch.Tensor = None,
    ) -> torch.FloatTensor:
        logits = outputs["encoder_rationale_logits"]
        labels = targets["rationale_labels"]
        passage_mask = targets["passage_mask"]

        return rationale_loss(
            logits,
            labels,
            passage_mask,
            self.max_rationale_length,
            reduction=self.reduction,
            mask=mask,
        )


class EncoderRationaleLoss(Loss):
    def __init__(self, max_rationale_length: int, reduction: str = "mean") -> None:
        self.max_rationale_length = max_rationale_length
        self.reduction = reduction

    def __call__(
        self,
        outputs,
        targets: Dict[str, torch.Tensor],
        mask: torch.Tensor = None,
    ) -> torch.FloatTensor:
        logits = outputs["rationale_logits"]
        labels = targets["rationale_labels"]
        passage_mask = targets["passage_mask"]

        return rationale_loss(
            logits,
            labels,
            passage_mask,
            self.max_rationale_length,
            reduction=self.reduction,
            mask=mask,
        )


def yes_no_gen_loss(
    logits: torch.FloatTensor,
    labels: torch.IntTensor,
    weight: Optional[torch.FloatTensor] = None,
    reduction="mean",
    mask: torch.Tensor = None,
) -> torch.FloatTensor:
    if mask is not None:
        logits = logits[mask.bool()]
        labels = labels[mask.bool()]

    if weight is not None:
        weight.to(logits.device)

    # loss = F.cross_entropy(logits, labels, weight=weight, reduction=reduction)
    loss = categorical_focal_loss_with_logits(
        logits, labels, weight=weight, reduction=reduction
    )
    return loss


class EncoderDecoderYNGLoss(Loss):
    def __init__(
        self, weight: Optional[torch.FloatTensor] = None, reduction: str = "mean"
    ) -> None:
        self.weight = weight
        self.reduction = reduction

    def __call__(
        self,
        outputs,
        targets: Dict[str, torch.Tensor],
        mask: torch.Tensor = None,
    ) -> torch.FloatTensor:
        logits = outputs["encoder_yng_logits"]
        labels = targets["yng_label"]

        return yes_no_gen_loss(
            logits, labels, weight=self.weight, reduction=self.reduction, mask=mask
        )


class EncoderYNGLoss(Loss):
    def __init__(
        self, weight: Optional[torch.FloatTensor] = None, reduction: str = "mean"
    ) -> None:
        self.weight = weight
        self.reduction = reduction

    def __call__(
        self,
        outputs,
        targets: Dict[str, torch.Tensor],
        mask: torch.Tensor = None,
    ) -> torch.FloatTensor:
        logits = outputs["yng_logits"]
        labels = targets["yng_label"]

        return yes_no_gen_loss(
            logits, labels, weight=self.weight, reduction=self.reduction, mask=mask
        )


class EncoderDecoderLoss(nn.Module):
    def __init__(
        self,
        max_rationale_length,
        yng_loss_weight=1.0,
        rationale_loss_weight=1.0,
        generative_loss_weight=1.0,
    ) -> None:
        super().__init__()

        self.yng_loss_weight = yng_loss_weight
        self.rationale_loss_weight = rationale_loss_weight
        self.generative_loss_weight = generative_loss_weight

        self.yes_no_gen_loss_fn = EncoderDecoderYNGLoss()
        self.rationale_loss_fn = EncoderDecoderRationaleLoss(
            max_rationale_length=max_rationale_length
        )
        self.generative_loss_fn = EncoderDecoderGenerativeLoss()

    def forward(
        self, outputs, targets: Dict[str, torch.Tensor]
    ) -> Tuple[torch.FloatTensor, Dict[str, float]]:
        is_generative = ~targets["yes_no"].bool()

        yng_loss = self.yes_no_gen_loss_fn(outputs, targets)
        rationale_loss = self.rationale_loss_fn(outputs, targets, mask=is_generative)
        generative_loss = self.generative_loss_fn(outputs, targets, mask=is_generative)

        total_loss = (
            self.yng_loss_weight * yng_loss
            + self.rationale_loss_weight * rationale_loss
            + self.generative_loss_weight * generative_loss
        )
        loss_logs = {
            "yng_loss": yng_loss.item(),
            "rationale_loss": rationale_loss.item(),
            "generative_loss": generative_loss.item(),
        }

        return total_loss, loss_logs
