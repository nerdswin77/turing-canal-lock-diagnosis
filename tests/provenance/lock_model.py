"""Exact (integer-minute) forward model of a canal with a chain of single-chamber locks. Used by the data generator only; the reference
solver carries its own copy of the same model.

A canal has locks 0 .. K-1 and nodes 0 .. K; lock k joins node k (its lower side) to node k+1 (its upper side). A vessel running up
passes locks from, from+1 .. to-1 and queues at the lower side of each; a vessel running down passes locks from-1, from-2 .. to and
queues at the upper side. A configuration maps a lock index to one departure from the nominal lock, and the key -1 to ("wait", w):
    ("loading", name)  the lock picks its next lockage by `name` instead of the nominal strict first-come-first-served
    ("valve", percent) the fill and empty time of the lock is `percent` per cent of the nominal one (valve partly blocked)
    ("gate", metres)   the usable chamber length is `metres` shorter (a gate leaf is out of service)
    ("crew", minutes)  every vessel needs `minutes` more to enter and `minutes` more to leave the chamber
    ("wait", w)        a lock starts a lockage that does not fill the chamber at most `w` minutes after it began to wait
"""
import heapq
import math

POLICIES = ["FIFO_STOP", "FIRST_FIT", "LARGEST", "SMALLEST", "LIFO"]
NON_NOMINAL = POLICIES[1:]
VALVE_PERCENTS = [110, 120, 130, 140, 150]
GATE_METRES = [10, 20, 30, 40]
CREW_MINUTES = [1, 2, 3, 4, 5]
WAITS = list(range(0, 20, 2))
GAP = 5                       # metres of clearance that every vessel needs in the chamber, in addition to its length
CD = 0.62                     # discharge coefficient of the filling valves
GRAVITY = 9.81


def vessel_path(v):
    """locks a vessel passes, in order"""
    if v["direction"] == "up":
        return list(range(v["from"], v["to"]))
    return list(range(v["from"] - 1, v["to"] - 1, -1))


def fill_minutes(lock, percent=100):
    """minutes to fill or empty the chamber of `lock` when the valves are blocked to the point that the effective valve area is
    100 / percent of its nominal value: Torricelli outflow through the valves, constant chamber area, constant head loss ignored"""
    area = lock["chamber_length"] * lock["chamber_width"]
    seconds = 2.0 * area * math.sqrt(lock["lift"]) / (CD * lock["valve"] * math.sqrt(2.0 * GRAVITY))
    return math.ceil(seconds * percent / 100.0 / 60.0)


def decode(line, config):
    K = line["n_locks"]
    policy, valve, gate, crew = ["FIFO_STOP"] * K, [100] * K, [0] * K, [0] * K
    wait = None
    for k, (kind, value) in config.items():
        if kind == "wait":
            wait = value
        elif kind == "loading":
            policy[k] = value
        elif kind == "valve":
            valve[k] = value
        elif kind == "gate":
            gate[k] = value
        else:
            crew[k] = value
    return policy, valve, gate, crew, wait


def _pack(policy, queue, cap, length, min_size):
    """vessels (arrival minute, vessel, position) taken into the next lockage, in loading order, and whether the chamber is full"""
    if policy in ("FIFO_STOP", "FIRST_FIT"):
        order = sorted(queue, key=lambda e: (e[0], e[1]))
    elif policy == "LARGEST":
        order = sorted(queue, key=lambda e: (-length[e[1]], e[0], e[1]))
    elif policy == "SMALLEST":
        order = sorted(queue, key=lambda e: (length[e[1]], e[0], e[1]))
    else:
        order = sorted(queue, key=lambda e: (-e[0], e[1]))
    used, batch = 0, []
    for e in order:
        size = length[e[1]] + GAP
        if used + size <= cap:
            batch.append(e)
            used += size
        elif policy == "FIFO_STOP":
            break
    return batch, len(batch) < len(queue) or cap - used < min_size


def simulate(line, config):
    """for every vessel the list, aligned with its path, of the minutes at which it has left the chambers of the locks of its path"""
    K, vessels, locks = line["n_locks"], line["vessels"], line["locks"]
    policy, valve, gate, crew, wait = decode(line, config)
    paths = [vessel_path(v) for v in vessels]
    length = [line["length"][v["class"]] for v in vessels]
    fill = [fill_minutes(locks[k], valve[k]) for k in range(K)]
    cap = [locks[k]["chamber_length"] - gate[k] for k in range(K)]
    min_size = min(line["length"]) + GAP
    leave = [[None] * len(p) for p in paths]
    waiting = [[[], []] for _ in range(K)]      # per lock and side: entries (arrival minute, vessel, position)
    side = [0] * K
    after = [0] * K                             # side of the chamber when its lockage or empty trip is over
    busy = [None] * K                           # minute at which the running lockage is over
    since = [0] * K                             # minute from which the chamber has been idle
    pending = [(v["release"], j, 0) for j, v in enumerate(vessels)]
    heapq.heapify(pending)
    t = 0
    while True:
        for k in range(K):
            if busy[k] is not None and busy[k] <= t:
                since[k], side[k], busy[k] = busy[k], after[k], None
        while pending and pending[0][0] <= t:
            arrived, j, q = heapq.heappop(pending)
            k = paths[j][q]
            waiting[k][0 if vessels[j]["direction"] == "up" else 1].append((arrived, j, q))
        expiry = []
        for k in range(K):
            if busy[k] is not None:
                continue
            queue = waiting[k][side[k]]
            if not queue:
                if waiting[k][1 - side[k]]:
                    busy[k], after[k] = t + fill[k], 1 - side[k]
                continue
            batch, full = _pack(policy[k], queue, cap[k], length, min_size)
            start = max(since[k], min(e[0] for e in queue))
            if not full and t < start + wait:
                expiry.append(start + wait)
                continue
            c = [vessels[e[1]]["class"] for e in batch]
            e_in = [line["enter"][x] + crew[k] for x in c]
            e_out = [line["exit"][x] + crew[k] for x in c]
            moment = t + sum(e_in) + fill[k]
            for e, x, cls in zip(batch, e_out, c):
                moment += x
                _, j, q = e
                leave[j][q] = moment
                if q + 1 < len(paths[j]):
                    node = k + 1 if vessels[j]["direction"] == "up" else k
                    heapq.heappush(pending, (moment + line["sail"][cls][node], j, q + 1))
                queue.remove(e)
            busy[k], after[k] = moment, 1 - side[k]
        cand = expiry + [b for b in busy if b is not None and b > t]
        if pending:
            cand.append(pending[0][0])
        cand = [x for x in cand if x > t]
        if not cand:
            if any(waiting[k][s] for k in range(K) for s in (0, 1)):
                raise ValueError("deadlock")
            break
        t = min(cand)
    return leave


def exits_at(line, leave, lock):
    """every minute at which a vessel has left the chamber of `lock`, in (vessel, position) order"""
    out = []
    for j, v in enumerate(line["vessels"]):
        for q, k in enumerate(vessel_path(v)):
            if k == lock:
                out.append(leave[j][q])
    return out
