import sys
from pathlib import Path

# Garante imports do pacote local durante pytest.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

