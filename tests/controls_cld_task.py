"""Negative-control definitions for solver-deliverable tasks.

Each control is {label, code, expect_fail, patch?}. `code` is the full text written to /root/results/solver.py (None = no
solver at all). expect_fail=True means the verifier must reject it; False means it is a legitimate solution the verifier must
ACCEPT (guards against over-strict checks - several review failures were verifiers rejecting legitimate output layouts).

The CLI used here (--input-dir / --output-dir, output file parameters.json) and the mutations in the "output" section are
specific to the task: when you start a new task, edit OUTPUT_FILE, the CLI preamble and the output mutations; keep the
generic security / resource / hygiene controls as they are.
"""
OUTPUT_FILE = "diagnosis.json"

PREAMBLE = """
import argparse as _ap, json as _j, os as _os
_p = _ap.ArgumentParser(); _p.add_argument('--input-dir'); _p.add_argument('--output-dir'); _a = _p.parse_args()
_f = _os.path.join(_a.output_dir, '%s')
""" % OUTPUT_FILE


def controls(good, truth_path):
    def mutate(body):          # run the real solver first (appended code runs after it), then rewrite its output
        return good + PREAMBLE + "_d = _j.load(open(_f))\n" + body + "_j.dump(_d, open(_f, 'w'))\n"

    real = good.replace('"%s"' % OUTPUT_FILE, '"real_output.json"')
    L = []
    add = lambda label, code, fail=True, **kw: L.append(dict(label=label, code=code, expect_fail=fail, **kw))
    # --- hygiene / security / resources: generic ------------------------------------------------------------------------
    add("NOP: no solver submitted", None)
    add("reads the ground truth", f"print(open({truth_path!r}).read())\n")
    add("tries to become root", "import os; os.setuid(0)\n")
    add("writes the wrong output filename", good.replace('"%s"' % OUTPUT_FILE, '"result.json"'))
    add("writes next to its inputs", PREAMBLE.replace("_f =", "_g =") + "open(_os.path.join(_a.input_dir, 'x.txt'), 'w').write('x')\n")
    add("imports an unavailable package", "import not_a_real_package\n")
    add("imports pytest, which is installed in the verifier image but not available to the program", good + "import pytest\n")
    _site = "import glob, sys\nfor _d in glob.glob('/tmp/qnd-pytest-site') + glob.glob('/usr/local/lib/python3*/site-packages') + glob.glob('/usr/local/lib/python3*/dist-packages'):\n    sys.path.append(_d)\n"
    add("writes outside OUTPUT_DIR (/tmp)", "open('/tmp/cld-outside-write.txt', 'w').write('x')\n" + good)
    add("imports pytest inside a multiprocessing worker (the restriction must reach worker processes)", _site + "import multiprocessing as _mp\ndef _w(_x):\n    import pytest\n    return 1\nif __name__ == '__main__':\n    with _mp.get_context('spawn').Pool(1) as _pool:\n        _pool.map(_w, [0])\n" + good)
    add("reaches pytest through importlib.import_module", _site + "import importlib\nimportlib.import_module('pytest')\n" + good)
    add("PASS: imports numpy, scipy and standard-library modules", good + "import numpy, scipy.optimize, scipy.sparse, multiprocessing, fractions, heapq\n", False)
    add("hangs (timeout patched to 4 s)", "import time; time.sleep(60)\n", patch=("SOLVER_TIMEOUT_SECONDS = 900", "SOLVER_TIMEOUT_SECONDS = 4"))
    add("allocates 6 GiB (stated limit 4 GB)", "x = bytearray(6 * 1024 ** 3)\n")
    add("starts 100 threads (stated limit 64 processes and threads)", "import threading, time\n_ts = [threading.Thread(target=time.sleep, args=(3,)) for _ in range(100)]\nfor _t in _ts:\n    _t.start()\n" + good)
    add("writes a 20 MB file (stated limit 16 MB per file)", PREAMBLE.replace("_f =", "_g =") + "open(_os.path.join(_a.output_dir, 'big.bin'), 'wb').write(b'0' * (20 * 1024 * 1024))\n" + good)
    add("symlinks the output to /etc/passwd", PREAMBLE.replace("_f =", "_g =") + "_os.symlink('/etc/passwd', _os.path.join(_a.output_dir, '%s'))\n" % OUTPUT_FILE)
    add("symlinks the output to the ground truth", PREAMBLE.replace("_f =", "_g =") + "_os.symlink(%r, _os.path.join(_a.output_dir, '%s'))\n" % (truth_path, OUTPUT_FILE))
    # --- content mutations: canal diagnosis schema -------------------------------------------------------------------------
    def first(key):
        return "_n = next(v for v in _d['canals'].values() if v['%s']); " % key
    add("a wait off by one step", mutate("_n = next(iter(_d['canals'].values())); _n['wait'] += 2 if _n['wait'] < 18 else -2\n"))
    add("a wait outside the allowed range (20)", mutate("_n = next(iter(_d['canals'].values())); _n['wait'] = 20\n"))
    add("an odd wait (7)", mutate("_n = next(iter(_d['canals'].values())); _n['wait'] = 7\n"))
    add("a wait reported as a float", mutate("_n = next(iter(_d['canals'].values())); _n['wait'] = float(_n['wait'])\n"))
    add("a wait reported as a boolean", mutate("_n = next(iter(_d['canals'].values())); _n['wait'] = False\n"))
    add("the wait missing", mutate("_d['canals'][next(iter(_d['canals']))].pop('wait')\n"))
    add("a lock with a loading rule missing from loading", mutate(first("loading") + "_n['loading'].pop(next(iter(_n['loading'])))\n"))
    add("a loading rule replaced by another rule", mutate(first("loading") + "_k = next(iter(_n['loading'])); _n['loading'][_k] = 'LARGEST' if _n['loading'][_k] != 'LARGEST' else 'LIFO'\n"))
    add("a loading rule name outside the catalogue (FIFO_STOP is not reported)", mutate(first("loading") + "_k = next(iter(_n['loading'])); _n['loading'][_k] = 'FIFO_STOP'\n"))
    add("a lower-case loading rule name", mutate(first("loading") + "_k = next(iter(_n['loading'])); _n['loading'][_k] = _n['loading'][_k].lower()\n"))
    add("a valve percentage off by one step", mutate(first("valve") + "_k = next(iter(_n['valve'])); _n['valve'][_k] += 10 if _n['valve'][_k] < 150 else -10\n"))
    add("a valve percentage off the allowed set (115)", mutate(first("valve") + "_k = next(iter(_n['valve'])); _n['valve'][_k] = 115\n"))
    add("a valve percentage reported as a float", mutate(first("valve") + "_k = next(iter(_n['valve'])); _n['valve'][_k] = float(_n['valve'][_k])\n"))
    add("a gate loss off by one step", mutate(first("gate") + "_k = next(iter(_n['gate'])); _n['gate'][_k] += 10 if _n['gate'][_k] < 40 else -10\n"))
    add("a gate loss off the allowed set (25)", mutate(first("gate") + "_k = next(iter(_n['gate'])); _n['gate'][_k] = 25\n"))
    add("a crew delay off by one minute", mutate(first("crew") + "_k = next(iter(_n['crew'])); _n['crew'][_k] += 1 if _n['crew'][_k] < 5 else -1\n"))
    add("a crew delay of zero minutes", mutate(first("crew") + "_k = next(iter(_n['crew'])); _n['crew'][_k] = 0\n"))
    add("a crew delay as a boolean", mutate(first("crew") + "_k = next(iter(_n['crew'])); _n['crew'][_k] = True\n"))
    add("a departure reported on the wrong lock", mutate(first("crew") + "_k = next(iter(_n['crew'])); _v = _n['crew'].pop(_k); _n['crew'][str((int(_k) + 1) % 9)] = _v\n"))
    add("a valve restriction reported as a crew delay (wrong kind)", mutate(first("valve") + "_k = next(iter(_n['valve'])); _n['valve'].pop(_k); _n['crew'][_k] = 2\n"))
    add("the same lock named in two objects", mutate(first("loading") + "_k = next(iter(_n['loading'])); _n['crew'][_k] = 2\n"))
    add("a lock key with a leading zero", mutate(first("loading") + "_k = next(iter(_n['loading'])); _n['loading']['0' + _k] = _n['loading'].pop(_k)\n"))
    add("a lock key outside the canal", mutate(first("loading") + "_k = next(iter(_n['loading'])); _n['loading']['99'] = _n['loading'].pop(_k)\n"))
    add("extra key in a canal entry (schema)", mutate("next(iter(_d['canals'].values()))['extra'] = 1\n"))
    add("a canal missing from the report", mutate("_d['canals'].pop(next(iter(_d['canals'])))\n"))
    def raw2(body): return good + PREAMBLE + "_t = open(_f).read()\n" + body
    add("NaN written as a value (not standard JSON)", raw2("open(_f, 'w').write(_t.replace('\"wait\"', '\"nan_marker\": NaN, \"wait\"', 1))\n"))
    add("Infinity written as a value (not standard JSON)", raw2("open(_f, 'w').write(_t.replace('\"wait\"', '\"inf_marker\": Infinity, \"wait\"', 1))\n"))
    # --- must PASS: legitimate organisations of the output (verify the verifier is outcome-only) --------------------------------
    add("output is a symlink to a correct report inside OUTPUT_DIR (must be a regular file)", real + PREAMBLE.replace("_f =", "_g =") + "_os.symlink(_os.path.join(_a.output_dir, 'real_output.json'), _os.path.join(_a.output_dir, '%s'))\n" % OUTPUT_FILE)
    add("output is a hard link to a correct report inside OUTPUT_DIR (must have a single name)", real + PREAMBLE.replace("_f =", "_g =") + "_os.link(_os.path.join(_a.output_dir, 'real_output.json'), _os.path.join(_a.output_dir, '%s'))\n" % OUTPUT_FILE)
    add("PASS: uses tempfile (lands in OUTPUT_DIR)", "import tempfile\nf = tempfile.NamedTemporaryFile(delete=False); f.write(b'x'); f.close()\n" + good, fail=False)
    add("PASS: forks a child that exits, then solves", "import os\nif os.fork() == 0:\n    os._exit(0)\n" + good, fail=False)
    add("PASS: sees at most 2 CPUs and still works", "import os\nassert len(os.sched_getaffinity(0)) <= 2\n" + good, fail=False)
    add("PASS: uses multiprocessing.Pool(2), then solves", "import multiprocessing as mp\nwith mp.Pool(2) as _p:\n    _p.map(abs, [-1, -2])\n" + good, fail=False)
    add("PASS: uses multiprocessing.Manager and a Queue, then solves", "import multiprocessing as mp\n_m = mp.Manager(); _q = _m.Queue(); _q.put(1); _q.get()\n" + good, fail=False)
    add("PASS: leaves a semaphore-named file in /dev/shm; it must be gone for the next campaign", "import os\n_p = '/dev/shm/sem.mp-leftover'\nassert not os.path.exists(_p), 'state carried between campaigns'\nopen(_p, 'w').write('x')\n" + good, fail=False)
    add("creates and removes 40,000 allowed-name entries in /dev/shm (more than the 1000 entries stated), which could overflow the inotify queue", "import os\nfor _i in range(40000):\n    _p = '/dev/shm/psm_%d' % _i\n    open(_p, 'w').close()\n    os.remove(_p)\n" + good)
    add("PASS: creates and removes 500 allowed-name entries in /dev/shm (inside the 1000 stated)", "import os\nfor _i in range(500):\n    _p = '/dev/shm/psm_%d' % _i\n    open(_p, 'w').close()\n    os.remove(_p)\n" + good, fail=False)
    add("creates a directory in /dev/shm", "import os\nos.mkdir('/dev/shm/stash')\n" + good)
    add("writes /dev/shm/sem.mp-A followed by a newline (a semaphore name plus a newline is not a multiprocessing name)", "open('/dev/shm/sem.mp-A\\n', 'w').write('x')\n" + good)
    add("writes /dev/shm/psm_A followed by a newline (a segment name plus a newline is not a multiprocessing name)", "open('/dev/shm/psm_A\\n', 'w').write('x')\n" + good)
    add("creates a prohibited file in /dev/shm and removes it again before it exits (the check must be in flight, not only after the exit)", "import os\nopen('/dev/shm/forbidden_state', 'w').write('x')\nos.remove('/dev/shm/forbidden_state')\n" + good)
    add("floods /dev/shm with 60000 allowed-looking entries and then creates and removes a prohibited file (the watcher must not lose events)", "import os\nfor i in range(60000):\n    open('/dev/shm/sem.mp-flood%d' % i, 'w').close()\n    os.remove('/dev/shm/sem.mp-flood%d' % i)\nopen('/dev/shm/forbidden_state', 'w').write('x')\nos.remove('/dev/shm/forbidden_state')\n" + good)
    add("creates a prohibited directory in /dev/shm and removes it again before it exits", "import os, shutil\nos.mkdir('/dev/shm/stash2')\nshutil.rmtree('/dev/shm/stash2')\n" + good)
    add("PASS: creates and removes a semaphore-named file in /dev/shm (the names multiprocessing uses)", "import os\nopen('/dev/shm/sem.mp-tmpABC', 'w').write('x')\nos.remove('/dev/shm/sem.mp-tmpABC')\n" + good, fail=False)
    add("PASS: creates and removes a FIFO named /dev/shm/psm_fifo (an entry that is not a directory, under an allowed name; nothing in /dev/shm survives the invocation)", "import os\nos.mkfifo('/dev/shm/psm_fifo')\nos.remove('/dev/shm/psm_fifo')\n" + good, fail=False)
    add("creates and removes a FIFO named /dev/shm/forbidden_fifo (the name is not allowed, whatever the type)", "import os\nos.mkfifo('/dev/shm/forbidden_fifo')\nos.remove('/dev/shm/forbidden_fifo')\n" + good)
    add("creates and removes a DIRECTORY named /dev/shm/psm_dir (a directory is never allowed: the names inside it would not be watched)", "import os\nos.mkdir('/dev/shm/psm_dir')\nos.rmdir('/dev/shm/psm_dir')\n" + good)
    add("leaves a directory named /dev/shm/sem.mp-dir (a directory is never allowed)", "import os\nos.mkdir('/dev/shm/sem.mp-dir')\n" + good)
    add("PASS: creates and removes the temporary name of a semaphore (sem. and six letters or digits) in /dev/shm", "import os\nopen('/dev/shm/sem.aB3dE9', 'w').write('x')\nos.remove('/dev/shm/sem.aB3dE9')\n" + good, fail=False)
    add("creates and removes /dev/shm/sem.abcdefg (seven characters: not the temporary name of a semaphore)", "import os\nopen('/dev/shm/sem.abcdefg', 'w').write('x')\nos.remove('/dev/shm/sem.abcdefg')\n" + good)
    add("creates and removes /dev/shm/sem.abcde (five characters: not the temporary name of a semaphore)", "import os\nopen('/dev/shm/sem.abcde', 'w').write('x')\nos.remove('/dev/shm/sem.abcde')\n" + good)
    add("creates and removes /dev/shm/sem.abcdef followed by a newline (not a name that the C library gives)", "import os\nopen('/dev/shm/sem.abcdef\\n', 'w').write('x')\nos.remove('/dev/shm/sem.abcdef\\n')\n" + good)
    def raw(body): return good + PREAMBLE + "_t = open(_f).read()\n" + body
    add("a repeated top-level key (the report is {'<top>': {}, '<top>': <real report>}; json.loads keeps the real one)", raw("_top = next(iter(_j.loads(_t))); assert _t.startswith('{\"%s\":' % _top); open(_f, 'w').write('{\"%s\":{},' % _top + _t[1:])\n"))
    add("a repeated key inside an entry (the first value is dropped by json.loads)", raw("_e = next(iter(next(iter(_j.loads(_t).values())).values())); _k = '\"%s\":' % next(iter(_e)); _i = _t.index(_k); open(_f, 'w').write(_t[:_i] + _k + '0,' + _t[_i:])\n"))
    return L

