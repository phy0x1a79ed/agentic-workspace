"""The daily sweep, as a console script rather than a CLI verb.

The timer must not depend on the gateway. A CLI verb reaches the service over
the hub, so a sweep scheduled at 03:30 would silently stop for as long as the
gateway happened to be down or mid-deploy, and the unit would report whatever
the CLI reported about a connection rather than about the archive. This entry
point imports the same modules the verb calls and writes the same run row.

Exit status is the unit's signal: non-zero when any session failed to move, so a
partial sweep is visible in ``systemctl --user status`` instead of only in the
run history.
"""

from __future__ import annotations

import json
import logging
import sys

from awm.transcripts import runs, sweep


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    runs.init()
    result = sweep.archive(days=sweep.DEFAULT_RETENTION_DAYS)
    result["run"] = runs.RunsDAO().record("sweep", result, trigger="timer")
    print(json.dumps(result))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
