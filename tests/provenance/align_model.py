"""Sorted-sequence alignment used by the generator to define the misfit of a hypothesis against the unlabeled readings.

A matched pair of a reading a and a predicted value b costs min(|a - b|, CAP); a skipped reading or a skipped prediction costs CAP.
There are at least as many predictions as readings: an instrument may fail to log some steps.

The alignment is computed in a band of BAND positions around the diagonals j - i = 0 .. n - m. This loses nothing for the acceptance
rules: a path that leaves the band skips more than BAND readings or predictions and costs more than BAND * CAP = 270, above every
margin the rules compare against (the true configuration's own misfit is at most 3 * CAP = 90, so a loss within 150 of it is below 270).
"""
import numpy as np

CAP = 30
BAND = 9
INF = 10 ** 6
OFFSETS = np.arange(31)


def best_offset_losses(obs, fin, resolution):
    """banded alignment loss of the sorted readings `obs` against the finish times `fin` (at least as many as readings) at each of the 31 clock offsets"""
    obs = np.asarray(obs, dtype=np.int64)
    pred = ((np.sort(np.asarray(fin, dtype=np.int64))[None, :] + OFFSETS[:, None]) // resolution) * resolution
    m, n = len(obs), pred.shape[1]
    D = n - m
    W = D + 2 * BAND + 1
    d = np.arange(-BAND, D + BAND + 1)
    dcost = (d + BAND) * CAP
    pad = np.zeros((len(OFFSETS), n + W + 2), dtype=np.int64)
    pad[:, BAND + 1:BAND + 1 + n] = pred
    dp = np.full((len(OFFSETS), W), INF, dtype=np.int64)
    dp[:, BAND:BAND + D + BAND + 1] = np.arange(D + BAND + 1) * CAP
    for i in range(1, m + 1):
        j = i + d
        valid = np.where((j >= 0) & (j <= n), 0, INF)
        mmask = np.where((j >= 1) & (j <= n), 0, INF)
        c = np.minimum(np.abs(obs[i - 1] - pad[:, i:i + W]), CAP)
        tmp = dp + c + mmask
        tmp[:, :-1] = np.minimum(tmp[:, :-1], dp[:, 1:] + CAP)
        tmp += valid
        dp = np.minimum.accumulate(tmp - dcost, axis=1) + dcost + valid
    return dp[:, BAND + D]
