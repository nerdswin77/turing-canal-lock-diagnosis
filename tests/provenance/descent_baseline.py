"""Adversary used when the data sets were drawn: a nearest-value misfit minimised by coordinate descent with random restarts, the waiting
time as one more coordinate, and the early stop of an explanation whose unmatched counts fit the statement. It reproduces the strategy of
a frontier-model solver observed on an earlier task of the same kind. A canal that this search reproduces exactly within its budget is
not used (see select_lines.py)."""
import random
import time
from collections import Counter

import numpy as np

from lock_model import CREW_MINUTES, GATE_METRES, NON_NOMINAL, VALVE_PERCENTS, WAITS, exits_at, simulate

OPTIONS = ([None] + [("loading", r) for r in NON_NOMINAL] + [("valve", s) for s in VALVE_PERCENTS] + [("gate", s) for s in GATE_METRES]
           + [("crew", s) for s in CREW_MINUTES])
KOPTS = list(WAITS)


class Simulator:
    def __init__(self, line, readings):
        self.line = line
        self.M = line["n_locks"]
        self.inst = line["instruments"]
        self.obs = [np.asarray(r, dtype=np.int64) for r in readings]
        self.last = None

    def config_of(self, x):
        cfg = {m: OPTIONS[v] for m, v in enumerate(x[:self.M]) if v}
        cfg[-1] = ("wait", KOPTS[x[self.M]])
        return cfg

    def score(self, x):
        dep = simulate(self.line, self.config_of(x))
        self.last = [np.sort(np.array(exits_at(self.line, dep, i["lock"]), dtype=np.int64)) for i in self.inst]
        total = 0
        for c, ins in enumerate(self.inst):
            u, obs, f = ins["resolution"], self.obs[c], self.last[c]
            pred = ((f[:, None] + np.arange(31)[None, :]) // u) * u
            idx = np.searchsorted(obs, pred)
            a = np.abs(pred - obs[np.minimum(idx, len(obs) - 1)])
            b = np.abs(pred - obs[np.maximum(idx - 1, 0)])
            total += int(np.minimum(np.minimum(a, b), 18).sum(axis=0).min())
        return total

    def valid(self, x):
        self.score(x)
        observed = missing = 0
        for c, ins in enumerate(self.inst):
            u, obs, f = ins["resolution"], self.obs[c], self.last[c]
            counts = Counter(map(int, obs))
            matched = max(sum(min(v, counts.get(k, 0)) for k, v in Counter(map(int, ((f + o) // u) * u)).items()) for o in range(31))
            observed += len(obs) - matched
            missing += len(f) - matched
        return observed == 0 and missing <= 3


def optimizer(sim, seconds, seed=31):
    rng = random.Random(seed)
    nm = sim.M
    t0 = time.monotonic()
    best, bestv = None, float("inf")
    trial = 0
    while time.monotonic() - t0 < seconds:
        if trial == 0 or best is None:
            x = [0] * nm + [rng.randrange(len(KOPTS))]
        elif trial % 5 == 0:
            x = [rng.randrange(len(OPTIONS)) if rng.random() < 0.35 else 0 for _ in range(nm)] + [rng.randrange(len(KOPTS))]
        else:
            x = list(best)
            for m in rng.sample(range(nm + 1), rng.randint(1, min(4, nm))):
                x[m] = rng.randrange(len(KOPTS)) if m == nm else rng.randrange(len(OPTIONS))
        v = sim.score(x)
        for _ in range(12):
            changed = False
            order = list(range(nm + 1))
            rng.shuffle(order)
            for m in order:
                old, bv, bi = x[m], v, x[m]
                for a in range(len(KOPTS) if m == nm else len(OPTIONS)):
                    if a == old:
                        continue
                    x[m] = a
                    w = sim.score(x)
                    if w < bv:
                        bv, bi = w, a
                x[m] = bi
                if bi != old:
                    changed, v = True, bv
                if time.monotonic() - t0 > seconds:
                    break
            if not changed or time.monotonic() - t0 > seconds:
                break
        if v < bestv:
            bestv, best = v, list(x)
            if bestv <= 252 and sim.valid(best):
                break
        trial += 1
    return best, bestv


def descent_diagnosis(line, readings, seconds=76, seed=31):
    sim = Simulator(line, readings)
    x, v = optimizer(sim, seconds, seed)
    nm = sim.M
    # necessity pruning: drop departures whose removal does not raise the misfit
    changed = True
    while changed:
        changed = False
        for m in range(nm):
            if x[m]:
                y = list(x)
                y[m] = 0
                if sim.score(y) <= v:
                    x, v, changed = y, sim.score(y), True
                    break
    cfg = sim.config_of(x)
    out = {"loading": {}, "valve": {}, "gate": {}, "crew": {}, "wait": cfg[-1][1]}
    name = {"loading": "loading", "valve": "valve", "gate": "gate", "crew": "crew"}
    for m in sorted(k for k in cfg if k >= 0):
        kind, val = cfg[m]
        out[name[kind]][str(m)] = val
    return out
