# Canal Lock Diagnosis

Write `/root/results/solver.py`, a reusable program that reads the logger readings of a canal with a chain of locks and works out what is wrong with the locks. For each canal it must find:

- the longest time that the locks wait for more vessels before they send off a part-filled chamber,
- which locks load their chamber by a rule other than strict first-come-first-served, and which rule,
- which locks have a partly blocked valve, and how strongly,
- which locks have a shortened chamber, and by how many metres,
- which locks have a slow crew, and by how many minutes.

## Inputs

`/root/data` holds the development campaign, which has two canals, S1 and S2. A campaign directory is laid out like this:

- `campaign.json`. Its key `canals` lists the canal IDs.
- `canal_<ID>.json` for every listed ID. It has the keys `canal_id`, `n_nodes`, `n_locks`, `n_vessels`, `length`, `enter`, `exit`, `sail`, `locks`, `vessels` and `instruments`.
  - Nodes are numbered from 0 to `n_nodes` minus 1 and locks from 0 to `n_locks` minus 1, and `n_nodes` is `n_locks` plus 1. Lock $ k $ joins node $ k $ (its lower side) to node $ k + 1 $ (its upper side) and has a single chamber. The water is higher at the upper side.
  - There are four vessel classes: 0 (large barge), 1 (medium barge), 2 (small barge) and 3 (pleasure craft). For a class $ c $, `length[c]` is the length of a vessel in metres, `enter[c]` is the number of minutes that a vessel needs to move into a chamber and moor, and `exit[c]` is the number of minutes that it needs to unmoor and leave the chamber. `sail[c][n]` is the number of minutes that a vessel of class $ c $ needs to sail from one lock to the next across node $ n $, for $ n $ from 1 to `n_locks` minus 1. The entries for node 0 and for node `n_locks` are 0 and are never used.
  - `locks` has one entry per lock with `lift` (the difference of the water levels in metres), `valve` (the effective area of the filling valves in square metres), `chamber_length` and `chamber_width` (in metres).
  - `vessels` lists the vessels in nondecreasing order of release time (ties can come in any order). The vessel at position $ j $ in the list is vessel $ j $, counting from 0. Each vessel has `direction` (`"up"` for increasing node numbers, `"down"` for decreasing), `class`, `from` and `to` (the nodes where its run starts and ends) and the integer minute `release`. An up vessel has `from` below `to` and passes the locks `from`, `from` + 1, up to `to` - 1, in that order, entering each from its lower side. A down vessel has `from` above `to` and passes the locks `from` - 1, `from` - 2, down to `to`, in that order, entering each from its upper side. Every vessel passes at least 4 locks.
  - `instruments` is sorted by increasing `lock` and has no lock twice. Each entry has `lock` and `resolution` (1, 2 or 5). The instrument at position $ c $ is instrument $ c $, counting from 0.
- `records_<ID>.npz`. Its arrays `readings_0`, `readings_1` and so on hold one list of integers per instrument, with `readings_c` belonging to instrument $ c $. Each list holds the readings that instrument logged, sorted in nondecreasing order. A reading does not tell you which vessel it belongs to.
- `development_key.json`. It gives the true diagnosis of S1 and S2 in the format of the required result below. Campaigns other than the development campaign do not have this file.

## How a canal works

All times are integer minutes counted from 0.

**Queues.** A vessel arrives at the first lock of its run at its release minute. When it has left a chamber and the lock was not the last of its run, it sails on and arrives at the next lock of its run after `sail[c][n]` minutes, where $ c $ is its class and $ n $ is the node between the two locks. It then waits in the queue of that lock on the side from which it enters. After leaving the last lock of its run it takes no further part.

**Chamber and lockage.** The chamber of a lock is at its lower or its upper side, and at minute 0 every chamber is idle at its lower side. A chamber is either idle at a side or busy. A vessel of class $ c $ takes up $ \text{length}[c] + 5 $ metres of the chamber, and a set of vessels fits if the sum of the lengths they take up is at most the usable length of the chamber, which is `chamber_length` minus the length lost to a shortened chamber (0 unless the lock has one).

The time $ F $ that a chamber needs to fill or to empty, in whole minutes, is

$$
F = \left\lceil \frac{1}{60} \cdot \frac{2 A \sqrt{h}}{C_d \, a \sqrt{2 g}} \cdot \frac{p}{100} \right\rceil
$$

where $ A $ is `chamber_length` times `chamber_width`, $ h $ is `lift`, $ a $ is `valve`, $ C_d = 0.62 $, $ g = 9.81 $ and $ p $ is 100 unless the valve of the lock is partly blocked (see below). A shortened chamber does not change $ A $ in this formula. The formula is evaluated in double precision, and no fill time of any canal lies within 0.01 minutes of a whole number.

Within one minute the order is: chambers whose lockage or empty trip ends at this minute become idle at their new side, then vessels arriving at this minute join the queues, then every idle chamber applies the rule below. The rule is applied afresh at every minute with the vessels then queued.

A chamber that is idle at a side $ s $ acts as follows.

1. No vessel is queued on side $ s $ and at least one vessel is queued on the other side. The chamber makes an empty trip: it is busy for $ F $ minutes and is then idle at the other side.
2. No vessel is queued on either side. Nothing happens.
3. At least one vessel is queued on side $ s $. The lock ranks the queued vessels by its loading rule (below) and takes them in that order as long as they fit together with the vessels already taken. The chamber is full if a queued vessel was not taken, or if the free length that is left is less than the length that the smallest class takes up, $ \text{length}[3] + 5 $. A full chamber starts the lockage at once. A chamber that is not full starts the lockage at the first minute $ t $ with $ t \ge b + w $, where $ w $ is the waiting time of the canal (see below) and $ b $ is the later of two minutes: the minute from which the chamber has been idle, and the earliest arrival minute among the vessels queued on side $ s $.

The loading rules rank the queued vessels as follows. In every ranking, a tie in the stated key goes to the vessel that arrived at this lock earlier, and after that to the lower vessel number.

- `FIFO_STOP`: by arrival minute at this lock. A vessel that does not fit stops the loading: the vessels behind it are not taken.
- `FIRST_FIT`: by arrival minute at this lock. A vessel that does not fit is skipped, and the next vessel that fits is taken.
- `LARGEST`: by decreasing length of the vessel. A vessel that does not fit is skipped.
- `SMALLEST`: by increasing length of the vessel. A vessel that does not fit is skipped.
- `LIFO`: by decreasing arrival minute at this lock, with ties to the lower vessel number. A vessel that does not fit is skipped.

When a lockage starts at minute $ t $ with the taken vessels $ v_1, \dots, v_m $ in the order of the ranking, the vessels move in one after another, the gates close, the chamber fills or empties, and the vessels leave one after another in the same order. The vessel $ v_i $ has left the chamber at minute

$$
t + \sum_{l = 1}^{m} \left( \text{enter}[c_l] + r \right) + F + \sum_{l = 1}^{i} \left( \text{exit}[c_l] + r \right)
$$

where $ c_l $ is the class of $ v_l $ and $ r $ is the extra time per vessel of a slow crew (0 unless the lock has one). The chamber is idle at the other side from the minute at which the last vessel has left.

## What can be wrong

In the nominal canal every lock uses `FIFO_STOP`, has $ p = 100 $, has no shortened chamber and has a crew with $ r = 0 $. In the actual canal, exactly 5 locks depart from nominal, and each of them does so in exactly one of four ways:

- its loading rule is one of `FIRST_FIT`, `LARGEST`, `SMALLEST` or `LIFO`,
- its valve is partly blocked, with $ p $ equal to 110, 120, 130, 140 or 150,
- its chamber is shortened by 10, 20, 30 or 40 metres,
- its crew is slow, with $ r $ equal to 1, 2, 3, 4 or 5 minutes.

A lock has at most one departure, and all the other locks are nominal. The waiting time $ w $ is an even integer from 0 to 18. It is the same for the whole canal, and you are not told its value.

## What the instruments report

An instrument at a lock logs every vessel that has left the chamber of that lock. It reports the minute of that event as the integer $ \lfloor (t + o) / v \rfloor v $. Here $ t $ is the minute at which the vessel has left the chamber, $ v $ is the resolution of the instrument, and $ o $ is an unknown integer clock offset between 0 and 30 that stays constant for that instrument.

An instrument can also fail to log a vessel. That vessel then has no entry in the list. Over all its instruments, a canal has between 0 and 3 vessels that were not logged. A reading never carries any other error, and readings of different vessels can be equal.

Every canal was built so that its true diagnosis explains the readings clearly better than any small change to it (dropping a departure, giving a lock another one, moving it to another lock, or another waiting time). When two explanations could both be fitted, the one meant is the one with fewer departures.

## Sizes and how the program is run

Every canal has 11 or 12 locks, 120 vessels and 3 or 4 instruments, and every instrument lock is passed by between 70 and 120 vessels. The program must handle three additional campaigns, not supplied here, each with exactly 6 canals, using this command:

```text
python3 -B solver.py --input-dir CAMPAIGN_DIR --output-dir OUTPUT_DIR
```

These campaigns use the same file layout and conventions as the development campaign, with different canals. Every canal, in the development campaign and in the additional campaigns, is drawn in the same way and is one whose true diagnosis a simple search does not find quickly. `solver.py` must be self-contained; no other submitted file is available to it.

The rules for running are:

- Campaign data are available only through `CAMPAIGN_DIR`, and the program must not modify them.
- Every file the program writes must go into `OUTPUT_DIR`, which exists, is empty and is writable. No other directory is writable for it, `/tmp` included, with one exception, `/dev/shm`: the program may create at most 1000 entries there, that are not directories and whose names start with `sem.mp-` or `psm_` followed only by letters, digits or underscores, or are exactly `sem.` followed by six letters or digits.
- The program must work when every file it leaves in `/dev/shm` is removed before and after every invocation, with no state carried from one invocation to the next.
- There is no network access. The program may import only the Python 3 standard library, numpy 2.4.4 and scipy 1.16.3.
- The program runs as an unprivileged user. Its working directory is read-only, and `HOME` and `TMPDIR` point to `OUTPUT_DIR`.
- Runs are deterministic: hash randomisation is turned off, the global `random` and `numpy.random` generators are seeded with 0, and numerical libraries use one thread.
- Each invocation must finish within 900 seconds of wall-clock time, measured from its start and not as CPU time. It runs on 2 CPUs and 4 GB of memory, with at most 64 processes and threads and no written file larger than 16 MB.

## Required result

When the program has written `diagnosis.json` into `OUTPUT_DIR`, it exits with status 0. The file must be a regular file with a single name, written directly in `OUTPUT_DIR`: a symbolic link, or a hard link to another file, is not a valid result. For example, with `--output-dir /root/results/development` it writes `/root/results/development/diagnosis.json`.

The file must be standard JSON (no NaN or Infinity), in which no object repeats a key. It is an object with the single key `canals`, whose value is an object with exactly one key per canal ID of the campaign. Each canal entry is an object with exactly these keys:

- `loading`: an object whose keys are the lock indices, written in decimal without leading zeros, of the locks whose loading rule is not `FIFO_STOP`. Its values are the rule names as strings. It has no other keys.
- `valve`: an object whose keys are the decimal indices of the locks with a partly blocked valve and whose values are their percentages $ p $ as integers.
- `gate`: an object whose keys are the decimal indices of the locks with a shortened chamber and whose values are the lost metres as integers.
- `crew`: an object whose keys are the decimal indices of the locks with a slow crew and whose values are the extra minutes $ r $ as integers.
- `wait`: the waiting time $ w $ as an integer.

A lock index may appear in at most one of the first four objects.

## Required accuracy

The program must meet every requirement below on every canal of the three additional campaigns; a result that meets only some of them does not meet the requirement.

- `loading`, `valve`, `gate` and `crew` have exactly the true keys, and every value equals the true value.
- `wait` equals the true waiting time.

The true values are the canal configurations that were used to generate each canal's readings.

You have 18000 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
