import sys
from pathlib import Path

# Make `harness_android` importable when running pytest from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
