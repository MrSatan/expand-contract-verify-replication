import glob
import json
import subprocess
import sys
import time

squawk_bin = sys.argv[1]
sql_dir = sys.argv[2]
files = sorted(glob.glob(f"{sql_dir}/*.sql"))

t0 = time.perf_counter()
p = subprocess.run([squawk_bin, "--reporter", "json"] + files, capture_output=True, text=True)
batch_ms = (time.perf_counter() - t0) * 1000.0

single_all = []
single_prisma = []
for f in files:
    ts = time.perf_counter()
    subprocess.run([squawk_bin, "--reporter", "json", f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dt = (time.perf_counter() - ts) * 1000.0
    single_all.append(round(dt, 2))
    if "/prisma_" in f:
        single_prisma.append(round(dt, 2))

print(json.dumps({
    "batch_ms": round(batch_ms, 2),
    "n_files": len(files),
    "single_all": single_all,
    "single_prisma": single_prisma,
    "diags_raw": p.stdout,
}))
