"""`python -m kalmux` behaves exactly like the `kalmux` command."""
import sys

from ._entry import main

if __name__ == "__main__":
    sys.exit(main())
