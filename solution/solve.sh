#!/bin/bash
set -euo pipefail

# The deliverable is a reusable program, so the oracle installs the reference solver at the required path ...
mkdir -p /root/results/development
cp /solution/solver.py /root/results/solver.py
chmod +x /root/results/solver.py

# ... and exercises it on the development campaign. The solver reads every campaign file (campaign.json, each canal_<ID>.json and
# records_<ID>.npz), simulates each canal exactly under hypothesised lock departures and writes the diagnosis.
python3 -B /root/results/solver.py --input-dir /root/data --output-dir /root/results/development

# Agentic check: compare the development diagnosis with the development answer key shipped with the data.
python3 - <<'PY'
import json
produced = json.load(open('/root/results/development/diagnosis.json'))['canals']
key = json.load(open('/root/data/development_key.json'))['canals']
assert sorted(produced) == sorted(key), 'solver output does not cover the campaign canals'
for sid in key:
    assert produced[sid] == key[sid], f'canal {sid}: {produced[sid]} != {key[sid]}'
print('development campaign matches the answer key:', sorted(produced))
PY
