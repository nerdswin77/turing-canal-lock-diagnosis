"""Deterministic data generator for canal-lock-diagnosis.

Usage:
    CLD_ROOT=<task root> python3 tests/provenance/generate_data.py dev|H1|H2|H3|all

Writes the development campaign to environment/data/ (plus its answer key) and each private campaign to tests/cases/<name>/ as a
checksummed tar.gz input archive plus truth.json. The ground truth is the configuration injected here; no solver produces it.
The loggers record the minute at which each vessel has left a lock chamber, without vessel labels, and may fail to log some vessels:
each logger yields the sorted list of its readings.

Acceptance rules (structural, evaluated on the TRUE configuration only, never on a solver's output):
  * every departure is necessary and distinguishable: every configuration obtained from the true one by dropping a departure, by giving a
    departing lock another departure, by moving a departure to another lock, or by changing the waiting time raises the alignment
    misfit by at least MARGIN_LOSS (a departure that leaves every reading unchanged is never injected);
  * the best clock offset of every logger is the true one and is unique (its misfit is smaller by at least OFFSET_MARGIN than that of
    every other offset).
"""
import gzip
import hashlib
import io
import json
import math
import os
import pickle
import random
import sys
import tarfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from align_model import best_offset_losses  # noqa: E402
from lock_model import (CREW_MINUTES, GATE_METRES, NON_NOMINAL, VALVE_PERCENTS, WAITS, exits_at, fill_minutes, simulate,  # noqa: E402
                        vessel_path)

ROOT = Path(os.environ.get("CLD_ROOT", Path(__file__).resolve().parents[2]))
N_VESSELS = 120
OFFSETS = np.arange(0, 31)
# Thresholds and what they rest on.
# CAP: a reading that is more than 30 minutes from its prediction is counted as 30. A lockage of the slowest chamber (fill 21 minutes, moors
#   and exits of up to 4 + 3 minutes per vessel) displaces an exit by about this much, so a wrongly predicted chamber content counts in full, while
#   one unlogged vessel (which shifts every later entry of its logger by one place) cannot dominate the misfit.
# MARGIN_LOSS: an alternative has to be worse than the truth by 150 = five readings displaced by the full CAP. Readings are exact up to their
#   resolution (1, 2 or 5 minutes), so an alternative within this margin would have to reproduce almost the whole log; the margin is more than ten
#   times the cost of a one-minute error of a reading and keeps the diagnosis unique with at most three unlogged vessels.
# OFFSET_MARGIN: the best clock offset must beat every other by 12. One minute of offset moves about a fifth of the readings of a logger (70 or more)
#   by at least one minute, so wrong offsets cost much more; the margin only excludes ties.
CAP = 30
MARGIN_LOSS = 150
OFFSET_MARGIN = 12
N_FAULTS = (int(os.environ.get("CLD_NF", 5)),) * 2
K_RANGE = (int(os.environ.get("CLD_KLO", 11)), int(os.environ.get("CLD_KHI", 12)))
LENGTH = [110, 85, 55, 12]    # metres: large barge, medium barge, small barge, pleasure craft
ENTER = [4, 3, 2, 1]          # minutes to move into the chamber and moor
EXIT = [3, 3, 2, 1]           # minutes to unmoor and leave the chamber
SAIL_FACTOR = [1.2, 1.0, 0.9, 0.6]
CLASS_WEIGHTS = [0.15, 0.30, 0.25, 0.30]
LOAD = 0.9                    # arrivals are paced so that the busiest lock is about 90 percent occupied by the generator's own estimate: a busy day on a main waterway
EFFECT = 12                   # a loading rule or a gate loss is only injected where it moves at least this many readings by more than CAP: 12 is a sixth of the 70 or more
                              # readings of a logger, enough to be seen at the logger and not a coincidence of rounding

CAMPAIGNS = {
    "dev": dict(seed=8001, canals=["S1", "S2"]),
    "H1": dict(seed=8101, canals=[f"A{i}" for i in range(1, 7)]),
    "H2": dict(seed=8202, canals=[f"B{i}" for i in range(1, 7)]),
    "H3": dict(seed=8303, canals=[f"C{i}" for i in range(1, 7)]),
}


def draw_locks(rng, K):
    """K locks; the valve area of each is chosen for a nominal fill time of 8 to 20 minutes and such that no fill time under any valve
    restriction lies within 0.01 minute of a whole minute (the minutes are rounded up from floating-point numbers)"""
    locks = []
    while len(locks) < K:
        length = rng.choice([180, 190, 200, 210])
        width = rng.choice([12.5, 14.0, 16.0])
        lift = round(rng.uniform(2.5, 9.0), 1)
        target = rng.uniform(8.0, 20.0) * 60.0
        valve = round(2.0 * length * width * math.sqrt(lift) / (0.62 * math.sqrt(2.0 * 9.81) * target), 1)
        lock = {"lift": lift, "valve": valve, "chamber_length": length, "chamber_width": width}
        if valve <= 0:
            continue
        ok = True
        for p in [100] + VALVE_PERCENTS:
            seconds = 2.0 * length * width * math.sqrt(lift) / (0.62 * valve * math.sqrt(2.0 * 9.81))
            x = seconds * p / 100.0 / 60.0
            if abs(x - round(x)) < 0.01:
                ok = False
        if ok and 8 <= fill_minutes(lock) <= 24:
            locks.append(lock)
    return locks


def draw_line(rng, sid):
    K = rng.randint(*K_RANGE)
    while True:
        locks = draw_locks(rng, K)
        reach = [rng.randint(10, 30) for _ in range(K + 1)]
        sail = [[0] + [max(1, round(reach[n] * SAIL_FACTOR[c])) for n in range(1, K)] + [0] for c in range(4)]
        vessels = []
        for _ in range(N_VESSELS):
            direction = rng.choice(["up", "down"])
            cls = rng.choices([0, 1, 2, 3], weights=CLASS_WEIGHTS)[0]
            if rng.random() < 0.6:
                a, b = 0, K
            else:
                while True:
                    a, b = sorted(rng.sample(range(K + 1), 2))
                    if b - a >= 4:
                        break
            if direction == "down":
                a, b = b, a
            vessels.append({"direction": direction, "class": cls, "from": a, "to": b})
        occupation = [0.0] * K
        for v in vessels:
            for k in vessel_path(v):
                occupation[k] += (fill_minutes(locks[k]) + 4.0) / 2.3
        gap = max(occupation) / (LOAD * N_VESSELS)
        now, i = 0.0, 0
        while i < N_VESSELS:                             # vessels reach their first lock in groups of a few at about the same minutes
            size = min(rng.randint(1, 4), N_VESSELS - i)
            for v in vessels[i:i + size]:
                v["release"] = int(round(now)) + rng.randint(0, 3)
            now += rng.expovariate(1.0 / (gap * size))
            i += size
        vessels.sort(key=lambda v: v["release"])
        line = {"canal_id": sid, "n_nodes": K + 1, "n_locks": K, "n_vessels": N_VESSELS, "length": LENGTH, "enter": ENTER, "exit": EXIT,
                "sail": sail, "locks": locks, "vessels": vessels}
        eligible = [k for k in range(K) if len(exits_at(line, simulate(line, {-1: ("wait", 6)}), k)) >= 25]   # 25: a lock with fewer exits is no logger site; the 70 to 120 of the statement is asserted by stated_violations
        n_inst = rng.randint(3, 4)
        if len(eligible) < n_inst:
            continue
        stations = sorted(rng.sample(eligible, n_inst))
        line["instruments"] = [{"lock": s, "resolution": rng.choice([1, 2, 5])} for s in stations]
        return line, [rng.randint(0, 30) for _ in stations]


def option_list():
    return ([("loading", r) for r in NON_NOMINAL] + [("valve", s) for s in VALVE_PERCENTS] + [("gate", s) for s in GATE_METRES]
            + [("crew", s) for s in CREW_MINUTES])


FORCED = {"A2": ("loading", "FIRST_FIT"), "B1": ("loading", "LARGEST"), "C2": ("loading", "SMALLEST"), "C4": ("loading", "LIFO")}


def draw_config(rng, line, sid=None):
    """four departures and a waiting time; a loading rule or a gate loss is only placed where it moves at least EFFECT readings of the line
    by more than CAP, given the other departures (a departure that never decides anything cannot be seen)"""
    K = line["n_locks"]
    kinds = [rng.choices(["loading", "valve", "gate", "crew"], weights=[0.3, 0.25, 0.2, 0.25])[0] for _ in range(rng.randint(*N_FAULTS))]
    forced = FORCED.get(sid)
    if forced is not None:
        kinds[0] = forced[0]
    sections = rng.sample(range(K), len(kinds))
    config = {-1: ("wait", rng.choice(WAITS))}
    pool = {"valve": VALVE_PERCENTS, "crew": CREW_MINUTES, "gate": GATE_METRES, "loading": NON_NOMINAL}
    for k, kind in zip(sections, kinds):
        if kind in ("valve", "crew"):
            config[k] = (kind, rng.choice(pool[kind]))
    for i, (k, kind) in enumerate(zip(sections, kinds)):
        if kind in ("valve", "crew"):
            continue
        base = [x for row in simulate(line, config) for x in row]
        good = []
        for o in ([forced] if forced is not None and i == 0 else [(kind, x) for x in pool[kind]]):
            trial = dict(config)
            trial[k] = o
            if sum(1 for x, y in zip([x for row in simulate(line, trial) for x in row], base) if abs(x - y) > CAP) >= EFFECT:
                good.append(o)
        if not good and forced is not None and i == 0:
            return None
        if not good:
            kind2 = rng.choice(["valve", "crew"])
            config[k] = (kind2, rng.choice(pool[kind2]))
        else:
            config[k] = rng.choice(good)
    return config


def true_readings(line, dep, offsets):
    """true reading of every departure at every detector: list over loggers of lists aligned with exits_at"""
    rows = []
    for inst, o in zip(line["instruments"], offsets):
        u = inst["resolution"]
        rows.append([((x + o) // u) * u for x in exits_at(line, dep, inst["lock"])])
    return rows


def finish_columns(line, dep):
    return [np.array(exits_at(line, dep, inst["lock"]), dtype=np.int64) for inst in line["instruments"]]


def misfit(line, dep, readings):
    """truncated sorted-alignment misfit of the readings, each detector minimised over its clock offset"""
    total = 0
    for fin, obs, inst in zip(finish_columns(line, dep), readings, line["instruments"]):
        total += int(best_offset_losses(obs, fin, inst["resolution"]).min())
    return total


def offsets_ok(line, dep, readings, offsets):
    for fin, obs, inst, o in zip(finish_columns(line, dep), readings, line["instruments"], offsets):
        losses = best_offset_losses(obs, fin, inst["resolution"])
        order = np.sort(losses)
        if int(np.argmin(losses)) != o or order[1] - order[0] < OFFSET_MARGIN:
            return False
    return True


def single_changes(line, config, additions=False):
    """(label, configuration) for the single changes of the configuration: a departing section takes another departure or loses its own,
    a departure moves to another section, the headway changes; with additions=True also a nominal section takes a departure"""
    K = line["n_locks"]
    opts = option_list()
    departing = [k for k in config if k >= 0]
    for k in departing:
        for o in opts:
            if config[k] != o:
                new = dict(config)
                new[k] = o
                yield ("replace", k), new
        new = dict(config)
        del new[k]
        yield ("drop", k), new
    for h in WAITS:
        if config[-1] != ("wait", h):
            new = dict(config)
            new[-1] = ("wait", h)
            yield ("wait", h), new
    for k1 in departing:
        for k2 in range(K):
            if k2 == k1 or k2 in config:
                continue
            for o in opts:
                new = dict(config)
                del new[k1]
                new[k2] = o
                yield ("move", k1, k2), new
    if additions:
        for k in range(K):
            if k in config:
                continue
            for o in opts:
                new = dict(config)
                new[k] = o
                yield ("add", k), new


def neighbours(line, config):
    """the alternatives that could be confused with the configuration: every single change, and every pair of changes that keeps the number
    of departures at most one above the configuration's (a headway change with any other change, two changes among the departing sections)"""
    seen = set()

    def emit(new):
        key = tuple(sorted(new.items()))
        if key not in seen:
            seen.add(key)
            yield new

    for _, new in single_changes(line, config):
        yield from emit(new)
    for h in WAITS:
        if config[-1] == ("wait", h):
            continue
        changed = dict(config)
        changed[-1] = ("wait", h)
        for label, new in single_changes(line, changed, additions=True):
            if label[0] != "wait":
                yield from emit(new)
    firsts = [(label, new) for label, new in single_changes(line, config) if label[0] in ("replace", "drop")]
    for la, a in firsts:
        for lb, b in single_changes(line, a):
            if lb[0] in ("replace", "drop") and lb[1] != la[1]:
                yield from emit(b)


def make_instance(rng, nprng, sid):
    while True:
        line, offsets = draw_line(rng, sid)
        config = draw_config(rng, line, sid)
        if config is None:
            continue
        dep = simulate(line, config)
        rows = true_readings(line, dep, offsets)
        n_lost = rng.randint(0, 3)
        entries = [(c, i) for c in range(len(rows)) for i in range(len(rows[c]))]
        lost = set(rng.sample(entries, n_lost))
        readings = [np.sort(np.array([rows[c][i] for i in range(len(rows[c])) if (c, i) not in lost], dtype=np.int64)) for c in range(len(rows))]
        if not offsets_ok(line, dep, readings, offsets):
            continue
        loss0 = misfit(line, dep, readings)
        if any(misfit(line, simulate(line, alt), readings) - loss0 < MARGIN_LOSS for alt in neighbours(line, config)):
            continue
        return line, config, readings, sorted(lost)


def truth_entry(config):
    return {
        "loading": {str(k): v for k, (kind, v) in sorted(config.items()) if kind == "loading"},
        "valve": {str(k): v for k, (kind, v) in sorted(config.items()) if kind == "valve"},
        "gate": {str(k): v for k, (kind, v) in sorted(config.items()) if kind == "gate"},
        "crew": {str(k): v for k, (kind, v) in sorted(config.items()) if kind == "crew"},
        "wait": config[-1][1],
    }


def line_json(line):
    return {k: line[k] for k in ("canal_id", "n_nodes", "n_locks", "n_vessels", "length", "enter", "exit", "sail", "locks", "vessels", "instruments")}


def stated_violations(line, truth):
    """Every bound that instruction.md states about a canal, checked on the canal as shipped (the dictionary of line_json) and on its truth
    entry (truth_entry plus the key lost_readings). Returns the list of violations; the generator asserts that it is empty before it writes a
    canal, and check_stated.py runs the same function over every shipped canal. It draws nothing from any random generator."""
    bad = []
    K = line["n_locks"]
    if K not in (11, 12) or line["n_nodes"] != K + 1:
        bad.append(f"locks {K} / nodes {line['n_nodes']}")
    if line["n_vessels"] != 120 or len(line["vessels"]) != 120:
        bad.append("number of vessels is not 120")
    if len(line["locks"]) != K or line["length"] != LENGTH or line["enter"] != ENTER or line["exit"] != EXIT:
        bad.append("lock list or class tables")
    inst = line["instruments"]
    if len(inst) not in (3, 4) or [i["lock"] for i in inst] != sorted({i["lock"] for i in inst}):
        bad.append("instruments: 3 or 4, sorted by lock, no lock twice")
    if any(i["resolution"] not in (1, 2, 5) or not 0 <= i["lock"] < K for i in inst):
        bad.append("instrument resolution or lock out of range")
    rel = [v["release"] for v in line["vessels"]]
    if rel != sorted(rel):
        bad.append("vessels not in nondecreasing order of release")
    for j, v in enumerate(line["vessels"]):
        if v["class"] not in (0, 1, 2, 3) or len(vessel_path(v)) < 4:
            bad.append(f"vessel {j}: class or fewer than 4 locks")
        if (v["direction"] == "up") != (v["from"] < v["to"]):
            bad.append(f"vessel {j}: direction does not match from/to")
    for c, i in enumerate(inst):
        passed = sum(1 for v in line["vessels"] if i["lock"] in vessel_path(v))
        if not 70 <= passed <= 120:
            bad.append(f"instrument {c} (lock {i['lock']}) is passed by {passed} vessels, not 70 to 120")
    if len(line["sail"]) != 4 or any(len(r) != K + 1 or r[0] != 0 or r[K] != 0 for r in line["sail"]):
        bad.append("sail table shape or its zero entries at nodes 0 and n_locks")
    n_dep = sum(len(truth[k]) for k in ("loading", "valve", "gate", "crew"))
    keys = [k for kind in ("loading", "valve", "gate", "crew") for k in truth[kind]]
    if n_dep != 5 or len(set(keys)) != 5:
        bad.append(f"{n_dep} departures on {len(set(keys))} different locks, not exactly 5")
    if truth["wait"] not in range(0, 19, 2):
        bad.append("waiting time is not an even integer from 0 to 18")
    if not 0 <= len(truth["lost_readings"]) <= 3:
        bad.append("not between 0 and 3 unlogged vessels")
    for k, lock in enumerate(line["locks"]):
        seconds = 2.0 * lock["chamber_length"] * lock["chamber_width"] * math.sqrt(lock["lift"]) / (0.62 * lock["valve"] * math.sqrt(2.0 * 9.81))
        for pct in [100] + VALVE_PERCENTS:
            x = seconds * pct / 100.0 / 60.0
            if abs(x - round(x)) < 0.01:
                bad.append(f"lock {k}: fill time at {pct} percent is within 0.01 minute of a whole number")
    return bad


def npz_bytes(readings):
    buf = io.BytesIO()
    np.savez(buf, **{f"readings_{c}": r.astype(np.int64) for c, r in enumerate(readings)})
    return buf.getvalue()


ACCEPT = None  # optional hook (select_lines.py): a drawn instance is kept only if the hook returns True


def build(name):
    cfg = CAMPAIGNS[name]
    files, truth = {}, {}
    files["campaign.json"] = (json.dumps({"canals": cfg["canals"]}, indent=2) + "\n").encode()
    cache = os.environ.get("CLD_CACHE")  # optional: accepted lines are kept here so that an interrupted run can resume
    for sid in cfg["canals"]:
        # every line slot has its own generator, so the slots are independent of each other
        rng = random.Random(f"{cfg['seed']}-{sid}")
        nprng = np.random.default_rng(cfg["seed"])
        cfile = Path(cache) / f"{name}_{sid}.pkl" if cache else None
        if cfile is not None and cfile.exists():
            line, config, readings, lost = pickle.loads(cfile.read_bytes())
        else:
            while True:
                line, config, readings, lost = make_instance(rng, nprng, sid)
                if stated_violations(line_json(line), dict(truth_entry(config), lost_readings=[[0, 0]] * len(lost))):
                    print("rejected a candidate for", name, sid, "(a bound of the statement)", flush=True)   # never happens for a shipped canal
                    continue
                if ACCEPT is None or ACCEPT(line, truth_entry(config), readings):
                    break
                print("rejected a candidate for", name, sid, flush=True)
            if cfile is not None:
                cfile.write_bytes(pickle.dumps((line, config, readings, lost)))
        entry = dict(truth_entry(config), lost_readings=[[line["instruments"][c]["lock"], i] for c, i in lost])
        violations = stated_violations(line_json(line), entry)           # also holds for a canal that was read back from the cache
        assert not violations, f"{name} {sid} breaks a bound that instruction.md states: {violations}"
        files[f"canal_{sid}.json"] = (json.dumps(line_json(line), separators=(",", ":")) + "\n").encode()
        files[f"records_{sid}.npz"] = npz_bytes(readings)
        truth[sid] = truth_entry(config)
        truth[sid]["lost_readings"] = [[line["instruments"][c]["lock"], i] for c, i in lost]
        print(name, sid, "K", line["n_locks"], "inst", [(i["lock"], i["resolution"]) for i in line["instruments"]], truth[sid], flush=True)
    return files, {"canals": truth}


def write_tar(path, files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for fname in sorted(files):
            data = files[fname]
            info = tarfile.TarInfo(fname)
            info.size, info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0o644, 0, 0, "root", "root"
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as gz:
        gz.write(buf.getvalue())
    path.write_bytes(out.getvalue())


def main(which):
    names = ["dev", "H1", "H2", "H3"] if which == "all" else [which]
    for name in names:
        files, truth = build(name)
        if name == "dev":
            data = ROOT / "environment" / "data"
            data.mkdir(parents=True, exist_ok=True)
            for fname, blob in files.items():
                (data / fname).write_bytes(blob)
            key = {"canals": {sid: {k: v for k, v in e.items() if k != "lost_readings"} for sid, e in truth["canals"].items()}}
            (data / "development_key.json").write_text(json.dumps(key, indent=2) + "\n")
            (ROOT / "tests" / "provenance" / "development_truth.json").write_text(json.dumps(truth, indent=2) + "\n")
        else:
            case = ROOT / "tests" / "cases" / name
            case.mkdir(parents=True, exist_ok=True)
            write_tar(case / f"{name}_input.tar.gz", files)
            (case / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")
            sums_path = ROOT / "tests" / "cases" / "archive_checksums.json"
            sums = json.loads(sums_path.read_text()) if sums_path.exists() else {}
            sums[name] = hashlib.sha256((case / f"{name}_input.tar.gz").read_bytes()).hexdigest()
            sums_path.write_text(json.dumps(dict(sorted(sums.items())), indent=2) + "\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
