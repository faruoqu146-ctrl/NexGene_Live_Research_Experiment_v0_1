"""Development helper for the recurring personal baseline learning cycle."""
import os
import sys

# Allow running from repo root: python scripts/learning_cycle.py
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

def main():
    print("NexGene learning cycle: schedule this worker in production.")
    print("Use authenticated API learning status endpoints for inspection.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
