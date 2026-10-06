"""Checks every bound that instruction.md states about a canal on all shipped canals: the development campaign and the private campaigns
(the archives tests/cases/H*/H*_input.tar.gz with their truth.json). Usage: python3 check_stated.py [TASK_ROOT]; exit status 1 on a violation."""
import io
import json
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate_data as G  # noqa: E402

root = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[1]
problems, n = [], 0


def campaigns():
    data = root / "environment" / "data"
    yield "dev", {p.name: p.read_bytes() for p in data.iterdir()}, json.loads((root / "tests/provenance/development_truth.json").read_text())["canals"]
    for name in ("H1", "H2", "H3"):
        with tarfile.open(root / "tests/cases" / name / f"{name}_input.tar.gz") as tf:
            files = {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}
        yield name, files, json.loads((root / "tests/cases" / name / "truth.json").read_text())["canals"]


for name, files, truth in campaigns():
    ids = json.loads(files["campaign.json"])["canals"]
    if sorted(ids) != sorted(truth) or len(ids) != (2 if name == "dev" else 6):
        problems.append(f"{name}: canal ids / count")
    for sid in ids:
        n += 1
        line = json.loads(files[f"canal_{sid}.json"])
        for v in G.stated_violations(line, truth[sid]):
            problems.append(f"{name}/{sid}: {v}")
print(f"{n} canals checked;", "no problems" if not problems else f"{len(problems)} problem(s)")
for q in problems:
    print("  ", q)
sys.exit(1 if problems else 0)
