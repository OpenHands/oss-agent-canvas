import json
import os

print("[github-pr] automation fired")
for key, value in sorted(os.environ.items()):
    if "EVENT" in key.upper() or "PAYLOAD" in key.upper():
        print(f"[github-pr] env {key} = {value[:400]}")
print("[github-pr] done")
