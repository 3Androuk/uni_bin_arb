"""Direct script entry point for the research monitor."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scr.app.cli import main


if __name__ == "__main__":
    main()
