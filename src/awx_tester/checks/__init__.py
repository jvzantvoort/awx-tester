"""Built-in checks. Importing this package registers them; within a level they run in this order."""

from . import platform  # noqa: I001,F401  (order matters: it is the run order)
from . import cluster, perf  # noqa: F401
from . import functional, load, resilience  # noqa: F401
