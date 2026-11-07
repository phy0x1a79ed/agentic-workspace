"""Write stanzas.json: the bytes Python mic leaves in the three files, per starting state.

The Python mic is gone from this tree. Run it in the awm env against a checkout of
release 49232bf9, which still holds awm/services/mic:
    mamba run -n awm python -I capture.py <that checkout>/awm/services/mic
Every input is synthetic.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from awm.mic import audio  # noqa: E402

FILES = [".config/pulse/daemon.conf", ".config/pulse/default.pa", ".asoundrc"]
STARTS = {
    "absent": None,
    "empty": "",
    "no_newline": "; operator line",
    "newline": "; operator line\n",
    "marked": "### awm virtmic: keep PulseAudio resident\n### awm virtmic: declare the virtual microphone sink\n# remote-audio bridge\n",
}

real_exists = os.path.exists


def main():
    out = []
    for include in (True, False):
        os.path.exists = lambda p, inc=include: inc if p.startswith("/etc/pulse/") else real_exists(p)
        for name, start in STARTS.items():
            for sink in ("virtmic", "othersink"):
                with tempfile.TemporaryDirectory() as home:
                    os.environ["HOME"] = home
                    for rel in FILES:
                        if start is not None:
                            p = Path(home, rel)
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_text(start)
                    changed = [audio.ensure_config(sink) for _ in range(2)]
                    files = {rel: Path(home, rel).read_text() for rel in FILES}
                out.append({"include": include, "start": start, "sink": sink, "changed": changed, "files": files})
    dest = Path(__file__).with_name("stanzas.json")
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(f"{len(out)} cases -> {dest}", file=sys.stderr)


main()
