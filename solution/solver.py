#!/usr/bin/env python3
"""Reference solver for canal-lock-diagnosis (unlabeled logger readings of a canal with a chain of locks).

For every canal it simulates the canal exactly under a hypothesised waiting time and set of lock departures (loading rule, blocked
valve, shortened chamber, slow crew), sorts the predicted chamber-leaving minutes of each logger, and compares them with the sorted
readings. The main search is a beam search that always explains the earliest unexplained reading first: it keeps the hypotheses that
reproduce most readings, counted from the start of the log, at the best clock offset. The order follows the canal: what leaves a lock reaches the next one after the sailing time, so the earliest unexplained reading points at the first lockage that goes wrong. A second search on a truncated alignment misfit
(a matched pair costs its difference truncated at 30 minutes, a skipped element of either list costs 30) runs if the first one stalls.
A departure that the records do not need is dropped at the end.
"""
import argparse
import heapq
import json
import math
import multiprocessing
import random
import time
from fractions import Fraction
from itertools import combinations
from pathlib import Path

import numpy as np

NON_NOMINAL = ["FIRST_FIT", "LARGEST", "SMALLEST", "LIFO"]
VALVE_PERCENTS = [110, 120, 130, 140, 150]
GATE_METRES = [10, 20, 30, 40]
CREW_MINUTES = [1, 2, 3, 4, 5]
OPTIONS = ([("loading", r) for r in NON_NOMINAL] + [("valve", s) for s in VALVE_PERCENTS] + [("gate", s) for s in GATE_METRES]
           + [("crew", s) for s in CREW_MINUTES])
OFFSETS = np.arange(0, 31)
WAITS = list(range(0, 20, 2))
GAP = 5
CAP = 30
BAND = 9
TOP_OFFSETS = 8
WORKERS = 2
_LINE = None
BEAM = 24
TRIPLE_SECONDS = 90.0
TRIPLE_TOP = 10
MOVES = False
PREFIX_BEAM = 10
PREFIX_STALL = 4
TOTAL_SECONDS = 840.0   # the invocation has 900 s; the lines share this budget
LINE_SECONDS = 150.0
PER_SECTION_SET = 3
INF = 10 ** 6
NEVER = -10 ** 9
BIG = 10 ** 9
T0 = time.monotonic()


def _arrival_key(e):
    return (e[0], e[1])


def _newest_key(e):
    return (-e[0], e[1])


def _pool_work(chunk):
    return _LINE.loss_chunk(chunk)


def _pool_prefix(chunk):
    return [_LINE.prefix_total(cfg) for cfg in chunk]


def prefix_len(obs, pred, u, missed):
    """number of readings, counted from the earliest, that the sorted prediction reproduces exactly at the best clock offset; up to
    `missed` predictions that lie below the next reading may be skipped (steps the instrument failed to log)"""
    first = obs[0]
    offsets = set()
    for j in range(min(missed, len(pred) - 1) + 1):
        for o in range(31):
            if ((pred[j] + o) // u) * u == first:
                offsets.add(o)
    best = 0
    n = len(pred)
    for o in offsets:
        pv = [((x + o) // u) * u for x in pred]
        j = skips = p = 0
        for x in obs:
            while j < n and pv[j] < x and skips < missed:
                j += 1
                skips += 1
            if j < n and pv[j] == x:
                j += 1
                p += 1
            else:
                break
        if p > best:
            best = p
    return best


def _fifo_key(e):
    return (e[1], e[2])


def band_loss(obs, pred):
    """obs (m,), pred (M, n) int32 with n >= m -> (M,) alignment loss restricted to a band around the diagonals j - i = 0 .. n - m"""
    m = len(obs)
    M, n = pred.shape
    D = n - m
    W = D + 2 * BAND + 1
    d = np.arange(-BAND, D + BAND + 1)
    dcost = ((d + BAND) * CAP).astype(np.int32)
    pad = np.zeros((M, n + W + 2), dtype=np.int32)
    pad[:, BAND + 1:BAND + 1 + n] = pred
    dp = np.full((M, W), INF, dtype=np.int32)
    dp[:, BAND:BAND + D + BAND + 1] = (np.arange(D + BAND + 1) * CAP).astype(np.int32)
    for i in range(1, m + 1):
        j = i + d
        valid = np.where((j >= 0) & (j <= n), 0, INF).astype(np.int32)
        mmask = np.where((j >= 1) & (j <= n), 0, INF).astype(np.int32)
        c = np.minimum(np.abs(obs[i - 1] - pad[:, i:i + W]), CAP)
        tmp = dp + c
        tmp += mmask
        np.minimum(tmp[:, :-1], dp[:, 1:] + CAP, out=tmp[:, :-1])
        tmp += valid
        new = np.minimum.accumulate(tmp - dcost, axis=1)
        new += dcost
        new += valid
        dp = new
    return dp[:, BAND + D]


class Line:
    def __init__(self, spec, readings, seconds=140.0):
        self.seconds = seconds
        self.M = spec["n_locks"]                        # number of locks (the search calls a lock's index `m`)
        self.vessels = spec["vessels"]
        self.n = len(self.vessels)
        self.locks = spec["locks"]
        self.length = spec["length"]
        self.enter = spec["enter"]
        self.exit = spec["exit"]
        self.sail = spec["sail"]
        self.inst = spec["instruments"]
        self.obs = [np.asarray(r, dtype=np.int32) for r in readings]
        self.obs_list = [[int(x) for x in r] for r in readings]
        self.dir = [0 if v["direction"] == "up" else 1 for v in self.vessels]
        self.cls = [v["class"] for v in self.vessels]
        self.size = [self.length[c] + GAP for c in self.cls]
        self.path = []
        for v in self.vessels:
            if v["direction"] == "up":
                self.path.append(list(range(v["from"], v["to"])))
            else:
                self.path.append(list(range(v["from"] - 1, v["to"] - 1, -1)))
        self.min_size = min(self.length) + GAP
        self.fills = {}
        for k, lock in enumerate(self.locks):
            area = lock["chamber_length"] * lock["chamber_width"]
            seconds_ = 2.0 * area * math.sqrt(lock["lift"]) / (0.62 * lock["valve"] * math.sqrt(2.0 * 9.81))
            for p in [100] + VALVE_PERCENTS:
                self.fills[(k, p)] = math.ceil(seconds_ * p / 100.0 / 60.0)
        self.visits = []
        for inst in self.inst:
            lk = inst["lock"]
            self.visits.append([(j, q) for j in range(self.n) for q, k in enumerate(self.path[j]) if k == lk])
        self.missed = [len(self.visits[c]) - len(self.obs[c]) for c in range(len(self.inst))]
        self.total = sum(len(o) for o in self.obs)
        self.cache = {}
        self.finish_cache = {}
        self.prefix_cache = {}
        self.pool = None
        self.deadline = float("inf")

    # ---- exact simulation ------------------------------------------------------------------------------------------
    def simulate(self, config):
        K, n = self.M, self.n
        policy, valve, gate, crew = ["FIFO_STOP"] * K, [100] * K, [0] * K, [0] * K
        wait = None
        for k, (kind, v) in config.items():
            if kind == "wait":
                wait = v
            elif kind == "loading":
                policy[k] = v
            elif kind == "valve":
                valve[k] = v
            elif kind == "gate":
                gate[k] = v
            else:
                crew[k] = v
        path, dirs, cls, size, length = self.path, self.dir, self.cls, self.size, self.length
        fill = [self.fills[(k, valve[k])] for k in range(K)]
        cap = [self.locks[k]["chamber_length"] - gate[k] for k in range(K)]
        enter, exit_, sail, min_size = self.enter, self.exit, self.sail, self.min_size
        leave = [[0] * len(p) for p in path]
        waiting = [[[], []] for _ in range(K)]
        side = [0] * K
        after = [0] * K
        busy = [None] * K
        since = [0] * K
        pending = [(self.vessels[j]["release"], j, 0) for j in range(n)]
        heapq.heapify(pending)
        t = 0
        while True:
            for k in range(K):
                b = busy[k]
                if b is not None and b <= t:
                    since[k], side[k], busy[k] = b, after[k], None
            while pending and pending[0][0] <= t:
                arrived, j, q = heapq.heappop(pending)
                waiting[path[j][q]][dirs[j]].append((arrived, j, q))
            nxt = BIG
            if pending:
                nxt = pending[0][0]
            for k in range(K):
                b = busy[k]
                if b is not None:
                    if b > t and b < nxt:
                        nxt = b
                    continue
                s = side[k]
                queue = waiting[k][s]
                if not queue:
                    if waiting[k][1 - s]:
                        busy[k], after[k] = t + fill[k], 1 - s
                        if busy[k] < nxt:
                            nxt = busy[k]
                    continue
                pol = policy[k]
                if pol == "FIFO_STOP" or pol == "FIRST_FIT":
                    order = sorted(queue, key=_arrival_key)
                elif pol == "LARGEST":
                    order = sorted(queue, key=lambda e: (-size[e[1]], e[0], e[1]))
                elif pol == "SMALLEST":
                    order = sorted(queue, key=lambda e: (size[e[1]], e[0], e[1]))
                else:
                    order = sorted(queue, key=_newest_key)
                used, batch = 0, []
                limit = cap[k]
                for e in order:
                    sz = size[e[1]]
                    if used + sz <= limit:
                        batch.append(e)
                        used += sz
                    elif pol == "FIFO_STOP":
                        break
                full = len(batch) < len(queue) or limit - used < min_size
                if not full:
                    first = queue[0][0]
                    for e in queue:
                        if e[0] < first:
                            first = e[0]
                    start = since[k] if since[k] > first else first
                    if t < start + wait:
                        if start + wait < nxt:
                            nxt = start + wait
                        continue
                cx = crew[k]
                moment = t + fill[k]
                for e in batch:
                    moment += enter[cls[e[1]]] + cx
                for e in batch:
                    _, j, q = e
                    c = cls[j]
                    moment += exit_[c] + cx
                    leave[j][q] = moment
                    if q + 1 < len(path[j]):
                        heapq.heappush(pending, (moment + sail[c][k + 1 if dirs[j] == 0 else k], j, q + 1))
                    queue.remove(e)
                busy[k], after[k] = moment, 1 - s
                if moment < nxt:
                    nxt = moment
            if nxt >= BIG:
                break
            t = nxt
        return leave

    # ---- agreement of the earliest readings -------------------------------------------------------------------------
    def prefix_total(self, config):
        """readings reproduced exactly from the start of the logs, summed over the instruments (all of them when the hypothesis explains
        the records)"""
        dep = self.simulate(config)
        total = 0
        for c, inst in enumerate(self.inst):
            pred = sorted(dep[j][q] for j, q in self.visits[c])
            total += prefix_len(self.obs_list[c], pred, inst["resolution"], self.missed[c])
        return total

    def prefix_many(self, configs):
        todo, seen = [], set()
        for cfg in configs:
            k = self.key_of(cfg)
            if k not in self.prefix_cache and k not in seen:
                seen.add(k)
                todo.append(cfg)
        if todo:
            if self.pool is not None and len(todo) >= 16:
                size = max(8, len(todo) // (4 * WORKERS) + 1)
                chunks = [todo[i:i + size] for i in range(0, len(todo), size)]
                results = self.pool.map(_pool_prefix, chunks, chunksize=1)
            else:
                chunks, results = [todo], [[self.prefix_total(c) for c in todo]]
            for ch, res in zip(chunks, results):
                for cfg, v in zip(ch, res):
                    self.prefix_cache[self.key_of(cfg)] = v
        return [self.prefix_cache[self.key_of(cfg)] for cfg in configs]

    def prefix_neighbours(self, config):
        for h in WAITS:
            if config[-1] != ("wait", h):
                new = dict(config)
                new[-1] = ("wait", h)
                yield new
        for k in range(self.M):
            for o in OPTIONS:
                if config.get(k) != o:
                    new = dict(config)
                    new[k] = o
                    yield new
            if k in config:
                new = dict(config)
                del new[k]
                yield new

    def prefix_search(self, deadline):
        """beam search that always explains the earliest unexplained reading first: the hypotheses that reproduce most readings from the
        start of the logs are kept (ties: fewer departures)"""
        beam = [{-1: ("wait", h)} for h in WAITS]
        scores = self.prefix_many(beam)
        best, best_score = max(zip(beam, scores), key=lambda x: x[1])
        stall = 0
        while best_score < self.total and stall < PREFIX_STALL and time.monotonic() < deadline:
            pool = {}
            for cfg in beam:
                for nb in self.prefix_neighbours(cfg):
                    pool.setdefault(self.key_of(nb), nb)
            cfgs = list(pool.values())
            vals = self.prefix_many(cfgs)
            order = sorted(range(len(cfgs)), key=lambda i: (-vals[i], sum(1 for k in cfgs[i] if k >= 0)))
            beam = [cfgs[i] for i in order[:PREFIX_BEAM]]
            if vals[order[0]] > best_score:
                best, best_score, stall = cfgs[order[0]], vals[order[0]], 0
            else:
                stall += 1
        return best, best_score

    def prune_exact(self, cfg):
        """drop every departure that the records do not need"""
        changed = True
        while changed:
            changed = False
            for k in sorted(m for m in cfg if m >= 0):
                base = dict(cfg)
                del base[k]
                if self.prefix_many([base])[0] >= self.total:
                    cfg, changed = base, True
                    break
        return cfg

    # ---- misfit ----------------------------------------------------------------------------------------------------
    @staticmethod
    def key_of(config):
        return tuple(sorted(config.items()))

    def sorted_finish(self, config):
        k = self.key_of(config)
        hit = self.finish_cache.get(k)
        if hit is None:
            finish = self.simulate(config)
            hit = [np.sort(np.array([finish[j][q] for j, q in self.visits[c]], dtype=np.int32)) for c, inst in enumerate(self.inst)]
            self.finish_cache[k] = hit
        return hit

    def loss_chunk(self, cfgs):
        """alignment misfit of every configuration in the list (runs in a worker process)"""
        cols = [self.sorted_finish(cfg) for cfg in cfgs]
        for cfg in cfgs:
            self.finish_cache.pop(self.key_of(cfg), None)
        totals = np.zeros(len(cfgs), dtype=np.int64)
        for c, inst in enumerate(self.inst):
            u = inst["resolution"]
            F = np.stack([col[c] for col in cols])
            obs = self.obs[c]
            pred = ((F[:, None, :] + OFFSETS[None, :, None]) // u) * u
            m = len(obs)
            at = np.minimum(np.searchsorted(obs, pred), m - 1)
            hits = (obs[at] == pred).sum(axis=2)
            pick = np.argpartition(-hits, TOP_OFFSETS, axis=1)[:, :TOP_OFFSETS]
            sel = np.take_along_axis(pred, pick[:, :, None], axis=1)
            h = sel.shape[0]
            loss = band_loss(obs, sel.reshape(h * TOP_OFFSETS, -1).astype(np.int32)).reshape(h, TOP_OFFSETS)
            totals += loss.min(axis=1)
        return [int(v) for v in totals]

    def evaluate_many(self, configs):
        todo, seen = [], set()
        for cfg in configs:
            k = self.key_of(cfg)
            if k not in self.cache and k not in seen:
                seen.add(k)
                todo.append(cfg)
        if todo:
            size = max(16, min(96, len(todo) // (4 * WORKERS) + 1))
            chunks = [todo[i:i + size] for i in range(0, len(todo), size)]
            if self.pool is not None and len(todo) >= 32:
                results = self.pool.map(_pool_work, chunks, chunksize=1)
            else:
                results = [self.loss_chunk(ch) for ch in chunks]
            for ch, res in zip(chunks, results):
                for cfg, v in zip(ch, res):
                    self.cache[self.key_of(cfg)] = v
        return [self.cache[self.key_of(cfg)] for cfg in configs]

    def evaluate(self, config):
        return self.evaluate_many([config])[0]

    # ---- search ----------------------------------------------------------------------------------------------------
    def neighbours(self, config):
        for k in WAITS:
            if config[-1] != ("wait", k):
                new = dict(config)
                new[-1] = ("wait", k)
                yield new
        for m in range(self.M):
            for o in OPTIONS:
                if config.get(m) != o:
                    new = dict(config)
                    new[m] = o
                    yield new
            if m in config:
                new = dict(config)
                del new[m]
                yield new
        for m1 in ([m for m in config if m >= 0] if MOVES else []):
            for m2 in range(self.M):
                if m2 == m1 or m2 in config:
                    continue
                for o in OPTIONS:
                    new = dict(config)
                    del new[m1]
                    new[m2] = o
                    yield new

    def search(self, seeds, width=BEAM):
        members = {self.key_of(c): c for c in seeds}
        vals = dict(zip(members, self.evaluate_many(list(members.values()))))
        best_key = min(vals, key=vals.get)
        best_cfg, best = members[best_key], vals[best_key]
        stall = 0
        for _ in range(40):
            if time.monotonic() > self.deadline:
                break
            pool = {}
            for cfg in members.values():
                for nb in self.neighbours(cfg):
                    pool.setdefault(self.key_of(nb), nb)
            if not pool:
                break
            cfgs = list(pool.values())
            scores = self.evaluate_many(cfgs)
            ranked = sorted(zip(scores, range(len(cfgs))))
            taken, per = [], {}
            for s, idx in ranked:
                cfg = cfgs[idx]
                sig = tuple(sorted(cfg.items()))[:0] + tuple(sorted(m for m in cfg if m >= 0)) + (cfg[-1][1],)
                if per.get(sig, 0) < PER_SECTION_SET:
                    per[sig] = per.get(sig, 0) + 1
                    taken.append(cfg)
                    if len(taken) == width:
                        break
            members = {self.key_of(c): c for c in taken}
            top = self.cache[self.key_of(taken[0])]
            if top < best:
                best_cfg, best, stall = taken[0], top, 0
            else:
                stall += 1
                if stall >= 3:
                    break
        return best_cfg, best

    def restarts(self, cfg, val):
        for _ in range(6):
            if time.monotonic() > self.deadline:
                break
            keys = sorted(m for m in cfg if m >= 0)
            seeds = []
            for r in range(1, min(3, len(keys)) + 1):
                for gone in combinations(keys, r):
                    seeds.append({k: e for k, e in cfg.items() if k not in gone})
            if not seeds:
                break
            cfg2, val2 = self.search(seeds)
            if val2 >= val:
                break
            cfg, val = cfg2, val2
        return cfg, val

    def pair_step(self, cfg, val):
        choices = [(m, o) for m in range(self.M) for o in OPTIONS + [None] if cfg.get(m) != o]
        choices += [(-1, ("wait", k)) for k in WAITS if cfg[-1] != ("wait", k)]
        cands = []
        for a, b in combinations(choices, 2):
            if a[0] == b[0]:
                continue
            new = dict(cfg)
            for m, o in (a, b):
                if o is None:
                    new.pop(m, None)
                else:
                    new[m] = o
            cands.append(new)
        best, best_cfg = val, None
        for lo in range(0, len(cands), 1500):
            if time.monotonic() > self.deadline:
                break
            chunk = cands[lo:lo + 1500]
            for new, v in zip(chunk, self.evaluate_many(chunk)):
                if v < best:
                    best, best_cfg = v, new
        return best_cfg, best

    def solve(self):
        global _LINE
        _LINE = self
        try:
            self.pool = multiprocessing.get_context("fork").Pool(WORKERS)
        except Exception:
            self.pool = None
        try:
            return self._solve()
        finally:
            if self.pool is not None:
                self.pool.terminate()
                self.pool.join()
                self.pool = None

    def triple_step(self, cfg, val, seconds, target):
        """best simultaneous change of three sections that currently deviate; every section takes its TRIPLE_TOP most promising states
        (ranked by the misfit of the single change) or keeps its own"""
        start_time = time.monotonic()
        sections = sorted(m for m in cfg if m >= 0)
        singles = {}
        for m in sections:
            options = [o for o in OPTIONS + [None] if o != cfg[m]]
            cands = []
            for o in options:
                new = dict(cfg)
                if o is None:
                    new.pop(m)
                else:
                    new[m] = o
                cands.append(new)
            scores = self.evaluate_many(cands)
            order = sorted(range(len(options)), key=lambda i: scores[i])[:TRIPLE_TOP]
            singles[m] = [cfg[m]] + [options[i] for i in order]
        best, best_cfg = val, None
        for trio in combinations(sections, 3):
            cands = []
            for a in singles[trio[0]]:
                for b in singles[trio[1]]:
                    for c in singles[trio[2]]:
                        new = dict(cfg)
                        for m, o in zip(trio, (a, b, c)):
                            if o is None:
                                new.pop(m, None)
                            else:
                                new[m] = o
                        cands.append(new)
            for new, v in zip(cands, self.evaluate_many(cands)):
                if v < best:
                    best, best_cfg = v, new
            if best_cfg is not None and best <= target:
                break
            if time.monotonic() - start_time > seconds:
                break
        return best_cfg, best

    def rescue(self, cfg, val, target, deadline):
        """descend from random starting hypotheses until the records are explained (misfit == target) or the line's time is up"""
        rnd = random.Random(12345)
        while val > target and time.monotonic() < deadline:
            start = {m: rnd.choice(OPTIONS) for m in rnd.sample(range(self.M), rnd.randint(3, 6))}
            start[-1] = ("wait", rnd.choice(WAITS))
            c, v = self.search([start], width=4)
            if v < val:
                cfg, val = self.restarts(c, v)
                if val > target:
                    cfg2, val2 = self.pair_step(cfg, val)
                    if cfg2 is not None:
                        cfg, val = self.search([cfg2])
        return cfg, val

    def prune(self, cfg, val):
        """the statement asks for the explanation with the fewest departures: a departure is dropped when the records are explained as
        well without it, possibly after one other single change (another headway, another state of a departing section)"""
        changed = True
        while changed:
            changed = False
            for m in sorted(k for k in cfg if k >= 0):
                base = dict(cfg)
                del base[m]
                cands = [base]
                for k in WAITS:
                    if base[-1] != ("wait", k):
                        new = dict(base)
                        new[-1] = ("wait", k)
                        cands.append(new)
                for m2 in sorted(k for k in base if k >= 0):
                    for o in OPTIONS:
                        if base[m2] != o:
                            new = dict(base)
                            new[m2] = o
                            cands.append(new)
                scores = self.evaluate_many(cands)
                best = min(range(len(cands)), key=lambda i: scores[i])
                if scores[best] <= val:
                    cfg, val, changed = cands[best], scores[best], True
                    break
        return cfg

    def _solve(self):
        deadline = self.deadline = time.monotonic() + self.seconds
        # the instruments missed exactly this many steps, so a hypothesis that explains the records has misfit CAP per missed step and no more
        target = CAP * sum(len(self.visits[c]) - len(self.obs[c]) for c in range(len(self.inst)))
        first, reached = self.prefix_search(deadline)
        if reached >= self.total:
            return self.report(self.prune_exact(first))
        cfg, val = self.search([{-1: ("wait", k)} for k in WAITS] + [first])
        for _ in range(4):
            if val <= target:
                break
            cfg, val = self.restarts(cfg, val)
            cfg2, val2 = self.pair_step(cfg, val)
            if cfg2 is None:
                break
            cfg, val = self.search([cfg2])
        if val > target:
            cfg2, val2 = self.triple_step(cfg, val, min(TRIPLE_SECONDS, max(0.0, deadline - time.monotonic())), target)
            if cfg2 is not None:
                cfg, val = self.search([cfg2])
                cfg, val = self.restarts(cfg, val)
        cfg, val = self.rescue(cfg, val, target, deadline)
        cfg = self.prune(cfg, val)
        return self.report(cfg)

    @staticmethod
    def report(cfg):
        out = {"loading": {}, "valve": {}, "gate": {}, "crew": {}, "wait": cfg[-1][1]}
        name = {"loading": "loading", "valve": "valve", "gate": "gate", "crew": "crew"}
        for m in sorted(cfg):
            if m < 0:
                continue
            kind, v = cfg[m]
            out[name[kind]][str(m)] = v
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    a = ap.parse_args()
    src, dst = Path(a.input_dir), Path(a.output_dir)
    ids = json.loads((src / "campaign.json").read_text())["canals"]
    result = {}
    for i, sid in enumerate(ids):
        spec = json.loads((src / f"canal_{sid}.json").read_text())
        with np.load(src / f"records_{sid}.npz") as rec:
            readings = [rec[f"readings_{c}"] for c in range(len(spec["instruments"]))]
        remaining = TOTAL_SECONDS - (time.monotonic() - T0)
        result[sid] = Line(spec, readings, min(LINE_SECONDS, remaining / (len(ids) - i))).solve()
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "diagnosis.json").write_text(json.dumps({"canals": result}, indent=2) + "\n")


if __name__ == "__main__":
    main()
