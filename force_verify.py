import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src" / "chimera" / "brains"))

from openworm_shards import bundle_summary


print("=" * 60)
print("SYSTEM DIAGNOSTICS")
print("=" * 60)
print(f"OS: {platform.system()} {platform.release()}")
print(f"Python Version: {sys.version}")
print(f"Executable: {sys.executable}")
print(f"Current Directory: {os.getcwd()}")
print("-" * 60)

base_dir = Path(__file__).resolve().parent
bundles_dir = base_dir / ".owm" / "bundles"
summary = bundle_summary(base_dir)

print("DIRECTORY AUDIT: {}".format(bundles_dir))
if bundles_dir.exists():
    for root, dirs, files in os.walk(bundles_dir):
        level = root.replace(str(bundles_dir), "").count(os.sep)
        indent = " " * 4 * level
        print(f"{indent}{os.path.basename(root)}/")
        subindent = " " * 4 * (level + 1)
        for filename in files:
            print(f"{subindent}{filename}")
else:
    print(f"[WARN] bundles_directory DOES NOT EXIST at: {bundles_dir}")

print("=" * 60)
print("INITIALIZING OWMETA BUNDLE LOAD")
print("=" * 60)

if not summary["present"]:
    print("[FAIL] OpenWorm graph bundle missing.")
    print(f"Expected graph directory: {summary['graphs_dir']}")
else:
    print("[OK] Connection: SUCCESS (raw graph shards)")
    print("[INFO] Querying Graph...")
    neuron_count = int(summary["neuron_count"])
    sample = list(summary["sample"])
    if neuron_count:
        print(f"[OK] TOTAL NEURONS IDENTIFIED: {neuron_count}")
        print(f"[INFO] DATA SAMPLE: {sample}")
    else:
        print("[WARN] DATA SILENCE: Graph shards were found, but no neuron name triples were detected.")

print("=" * 60)
print("DEBUG COMPLETE")
