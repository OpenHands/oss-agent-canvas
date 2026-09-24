import subprocess
import sys

sys.path.insert(0, "skills/github-stale-ci-pr-closer/scripts")
from worker import complete_run

result = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", "skills/github-stale-ci-pr-closer/tests", "-v"],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
print(result.stdout, flush=True)
complete_run("COMPLETED" if result.returncode == 0 else "FAILED", None if result.returncode == 0 else "Lifecycle validation failed")
raise SystemExit(result.returncode)
