#!/usr/bin/env bash
# A singleton reads its own secrets and no other singleton's (fleet.md R99 item 1).
#
# Runs two transient system units the way awm-plugin@<tool> runs a singleton:
# each as its own user, each loading its own root-only secrets file with
# LoadCredential=secrets.env:<file>, each reading it through
# awm.gatewayclient.credentials. Then each tries the other's secret: the
# source file, the credential systemd made for the other unit, and the other
# unit's environment.
#
# Needs root through sudo and systemd 247 or later. Run it only when asked; it
# is never part of the fast smoke. It leaves nothing behind: the units are
# transient and their users are dynamic.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
loader="$repo/awm/service_components/gatewayclient/awm/gatewayclient/credentials.py"

src="$(sudo mktemp -d /etc/awm-secrets-test.XXXXXX)"
lib="$(mktemp -d /tmp/awm-secrets-lib.XXXXXX)"
tag="$(basename "$src" | cut -d. -f2 | tr 'A-Z' 'a-z')"
unit_a="awm-secrets-test-a-$tag"
unit_b="awm-secrets-test-b-$tag"
cleanup() {
    sudo systemctl stop "$unit_a.service" "$unit_b.service" 2>/dev/null || true
    sudo rm -rf "${src:?}"
    rm -rf "${lib:?}"
}
trap cleanup EXIT

# The loader alone, where a dynamic user can import it: the repo is under a home
# the singletons do not see.
cp "$loader" "$lib/credentials.py"
chmod -R a+rX "$lib"

secret_a="a-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
secret_b="b-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf 'AWM_TEST_SECRET=%s\n' "$secret_a" | sudo tee "$src/a.env" >/dev/null
printf 'AWM_TEST_SECRET=%s\n' "$secret_b" | sudo tee "$src/b.env" >/dev/null
sudo chmod 0700 "$src"
sudo chmod 0600 "$src/a.env" "$src/b.env"

singleton() {  # singleton <unit> <own secrets file> <command...>
    local unit="$1" file="$2"
    shift 2
    sudo systemd-run --quiet --unit "$unit" --wait --pipe --collect \
        -p DynamicUser=yes -p ProtectSystem=strict -p ProtectHome=tmpfs -p PrivateTmp=yes \
        -p "BindReadOnlyPaths=$lib" -p "LoadCredential=secrets.env:$file" \
        -E "PYTHONPATH=$lib" "$@"
}

fail=0
check() {  # check <what> <expected> <got>
    if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected '$2', got '$3'"; fail=1; fi
}

read_own='import credentials, os; credentials.load(); print(os.environ.get("AWM_TEST_SECRET", ""))'
got="$(singleton "$unit_a" "$src/a.env" /usr/bin/python3 -c "$read_own")"
check "a singleton reads its own secret" "$secret_a" "$got"
got="$(singleton "$unit_b" "$src/b.env" /usr/bin/python3 -c "$read_own")"
check "the other singleton reads its own" "$secret_b" "$got"

got="$(singleton "$unit_b" "$src/b.env" /bin/sh -c "cat '$src/a.env' 2>/dev/null || echo refused")"
check "a singleton cannot read another's secrets file" refused "$got"

# a holds its credential, and its secret in its environment, while b looks.
sudo systemd-run --quiet --unit "$unit_a" --collect \
    -p DynamicUser=yes -p "BindReadOnlyPaths=$lib" -p "LoadCredential=secrets.env:$src/a.env" \
    -E "PYTHONPATH=$lib" /usr/bin/python3 -c \
    'import credentials, time; credentials.load(); import os; os.execv("/bin/sleep", ["sleep", "60"])'
sleep 1
held="/run/credentials/$unit_a.service/secrets.env"
sudo test -f "$held" || { echo "FAIL a's credential is not at $held"; fail=1; }
got="$(singleton "$unit_b" "$src/b.env" /bin/sh -c "cat '$held' 2>/dev/null || echo refused")"
check "a singleton cannot read another's loaded credential" refused "$got"
pid="$(systemctl show -p MainPID --value "$unit_a.service")"
sudo grep -qa "$secret_a" "/proc/$pid/environ" || { echo "FAIL a's secret is not in its environment"; fail=1; }
got="$(singleton "$unit_b" "$src/b.env" /bin/sh -c "cat /proc/$pid/environ 2>/dev/null || echo refused")"
check "a singleton cannot read another's environment" refused "$got"

exit "$fail"
