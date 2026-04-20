import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src" / "chimera" / "brains"))

from openworm_shards import bundle_summary


base_path = Path(os.getcwd())
bundles_path = base_path / ".owm" / "bundles"
summary = bundle_summary(base_path)

print("--- Chimera Worm: Deep Diagnostics ---")
print(f"Checking Path: {bundles_path}")

if not summary["present"]:
    print(f"[X] FOLDER MISSING: I can't find the graph bundle at {summary['graphs_dir']}")
else:
    print("[OK] Bundle folder found.")
    print("Reading the worm graph shards directly...")
    print(f"[OK] SUCCESS! Found {summary['neuron_count']} neurons in the Chimera brain.")
    print(f"[INFO] Sample neurons: {summary['sample']}")
