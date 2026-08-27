"""Switch the public service set on in sirius's `enabled.json`, additively.

Called by `install-awm.sh`. Separate from it because the seeding block there
only ever runs on a box that has no `enabled.json` yet: adding a service to
`PUBLIC_SERVICES` on a box that already has the file would otherwise install
the service, report success, and never start it.

Additive on purpose. Names not passed here are left exactly as they are —
the file is edited from the box as well as from this repo, and a rewrite from
the repo's idea of the set would switch off whatever someone turned on there.

    enable-services.py <enabled.json> <name> [<name> …]
"""

import json
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path, names = sys.argv[1], sys.argv[2:]

    with open(path) as fh:
        doc = json.load(fh)

    changed = [n for n in names if doc.get(n) is not True]
    if not changed:
        return 0

    doc.update({n: True for n in names})
    with open(path, "w") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"   enabled: {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
