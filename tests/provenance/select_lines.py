"""Builds every data set - the development campaign and the three private campaigns alike - with two acceptance filters on top of the
identifiability rules of generate_data.py. A drawn canal is kept only if (1) the first search stage of the reference solver (solution/solver.py),
the beam over the earliest readings, explains every reading within 120 s (a feasibility guard that uses no hidden answer: the stage can see by
itself whether it has explained the whole log; 120 s is most of the 150 s that a 900 s run leaves for each of the 6 canals of a campaign), and
(2) the nearest-value descent search of descent_baseline.py does NOT reproduce the truth within 110 s (the same budget minus the time to read the
files and write the answer).
Both filters apply to the development canals too, so that the development canals are drawn in the same way as the graded ones."""
import sys, time, pathlib
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate_data as G
import descent_baseline as D
sys.path.insert(0, str(HERE.parent.parent / "solution"))
import solver as R

FILTER = True


def reference_ok(line, readings):
    """truth-free: the first stage must explain every reading (reached >= total) within 120 s"""
    t0 = time.monotonic()
    ref = R.Line(G.line_json(line), readings)
    best, reached = ref.prefix_search(time.monotonic() + 120)
    ok = reached >= ref.total
    print("  reference first stage", "explains every reading" if ok else "does not finish", round(time.monotonic() - t0), "s", flush=True)
    return ok


def accept(line, truth, readings):
    if not reference_ok(line, readings):
        return False
    if not FILTER:                                  # only for experiments; the shipped data sets are built with FILTER on
        return True
    t0 = time.monotonic()
    solved = D.descent_diagnosis(G.line_json(line), readings, 110) == truth
    print("  descent baseline", "EXACT (candidate dropped)" if solved else "misses", round(time.monotonic() - t0), "s", flush=True)
    return not solved


if __name__ == "__main__":
    G.ACCEPT = accept
    FILTER = "--no-descent" not in sys.argv
    sys.argv = [a for a in sys.argv if a != "--no-descent"]
    G.main(sys.argv[1] if len(sys.argv) > 1 else "all")
