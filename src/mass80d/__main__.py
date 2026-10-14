"""Entry point: `python -m mass80d`. All logic lives in daemon.py."""

from .daemon import main

raise SystemExit(main())
