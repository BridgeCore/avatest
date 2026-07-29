import sys
from pathlib import Path

# Ensure `src` is importable as a package regardless of how pytest is invoked
# (plain `pytest` does not add the cwd to sys.path the way `python -m pytest` does).
sys.path.insert(0, str(Path(__file__).parent))
