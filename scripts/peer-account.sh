#!/usr/bin/env bash
# Install (or remove) the contained `awmpeer` login a peer node's agents ssh
# into: a shell with no sudo, no ssh forwarding and no outbound connections,
# loopback included. Idempotent. Run as root on the node being entered.
#
#   sudo scripts/peer-account.sh [--key PUBKEY_FILE ...] [--from CIDR[,CIDR]] [--cred FILE]
#   sudo scripts/peer-account.sh --remove
#
# --from pins every key to the peer's source address. --cred mirrors this
# node's peer-credential file to /var/lib/awmpeer/peer_cred, readable by
# awmpeer, so the peer's `cat "$AWM_PEER_CRED"` fetch works without awmpeer
# ever traversing the operator's home.
set -euo pipefail

PEER=awmpeer
SSHD_DROPIN=/etc/ssh/sshd_config.d/zz-awmpeer.conf
NFT_FILE=/etc/awmpeer/egress.nft
EGRESS_UNIT=/etc/systemd/system/awmpeer-egress.service
CRED_DIR=/var/lib/awmpeer
CRED_SVC=/etc/systemd/system/awmpeer-cred.service
CRED_PATH=/etc/systemd/system/awmpeer-cred.path

KEYS=() FROM="" CRED="" REMOVE=0
while [ $# -gt 0 ]; do
    case $1 in
        --key) KEYS+=("$2"); shift 2 ;;
        --from) FROM=$2; shift 2 ;;
        --cred) CRED=$(readlink -f "$2"); shift 2 ;;
        --remove) REMOVE=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

step() { echo "== $*"; }

reload_sshd() {
    mkdir -p /run/sshd
    sshd -t || return 1
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
}

if [ "$REMOVE" = 1 ]; then
    step "remove"
    systemctl disable --now awmpeer-cred.path awmpeer-egress.service 2>/dev/null || true
    rm -f "$CRED_SVC" "$CRED_PATH" "$EGRESS_UNIT" "$SSHD_DROPIN"
    systemctl daemon-reload
    nft delete table inet "$PEER" 2>/dev/null || true
    reload_sshd
    pkill -u "$PEER" 2>/dev/null || true
    id "$PEER" >/dev/null 2>&1 && userdel -r "$PEER" 2>/dev/null || true
    rm -rf /etc/awmpeer "$CRED_DIR"
    exit 0
fi

[ ${#KEYS[@]} -gt 0 ] || echo "no --key given: $PEER gets an empty authorized_keys" >&2
for k in "${KEYS[@]+"${KEYS[@]}"}"; do ssh-keygen -lf "$k" >/dev/null || { echo "not a public key: $k" >&2; exit 1; }; done
[ -z "$CRED" ] || [ -f "$CRED" ] || { echo "no such credential file: $CRED" >&2; exit 1; }

step "user $PEER"
id "$PEER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --user-group "$PEER"
usermod -G "" "$PEER"
passwd -l "$PEER" >/dev/null
[ "$(id -nG "$PEER")" = "$PEER" ] || { echo "$PEER has extra groups: $(id -nG "$PEER")" >&2; exit 1; }
HOME_DIR=$(getent passwd "$PEER" | cut -d: -f6)
PEER_UID=$(id -u "$PEER")
chown "$PEER:$PEER" "$HOME_DIR"
chmod 700 "$HOME_DIR"

step "other homes closed to $PEER"
for h in /home/*/; do
    h=${h%/}
    [ "$h" = "$HOME_DIR" ] && continue
    chmod o-rwx "$h"
done

step "authorized_keys"
OPTS="restrict,pty"
[ -n "$FROM" ] && OPTS="$OPTS,from=\"$FROM\""
install -d -m 755 -o root -g root "$HOME_DIR/.ssh"
TMP_KEYS=$(mktemp)
for k in "${KEYS[@]+"${KEYS[@]}"}"; do
    grep -Ev '^\s*(#|$)' "$k" | while read -r line; do echo "$OPTS $line"; done
done > "$TMP_KEYS"
install -m 644 -o root -g root "$TMP_KEYS" "$HOME_DIR/.ssh/authorized_keys"
rm -f "$TMP_KEYS"

step "sshd"
TMP_DROPIN=$(mktemp)
cat > "$TMP_DROPIN" <<EOF
Match User $PEER
    DisableForwarding yes
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
    PermitListen none
    PermitOpen none
    PermitUserRC no
    AuthenticationMethods publickey
EOF
if ! cmp -s "$TMP_DROPIN" "$SSHD_DROPIN" 2>/dev/null; then
    PREV=$(mktemp); cp -p "$SSHD_DROPIN" "$PREV" 2>/dev/null || : > "$PREV"
    install -m 644 "$TMP_DROPIN" "$SSHD_DROPIN"
    if ! reload_sshd; then
        if [ -s "$PREV" ]; then install -m 644 "$PREV" "$SSHD_DROPIN"; else rm -f "$SSHD_DROPIN"; fi
        rm -f "$TMP_DROPIN" "$PREV"
        echo "sshd -t rejected the drop-in; restored the previous state" >&2
        exit 1
    fi
    rm -f "$PREV"
fi
rm -f "$TMP_DROPIN"

step "egress"
install -d -m 755 /etc/awmpeer
cat > "$NFT_FILE" <<EOF
table inet $PEER {}
delete table inet $PEER
table inet $PEER {
    chain output {
        type filter hook output priority filter; policy accept;
        meta skuid $PEER_UID jump peer_out
    }
    chain peer_out {
        ct state established,related accept
        counter drop
    }
}
EOF
cat > "$EGRESS_UNIT" <<EOF
[Unit]
Description=Drop new outbound connections from $PEER
Before=network-pre.target ssh.service
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $NFT_FILE
ExecStop=/usr/sbin/nft delete table inet $PEER

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable awmpeer-egress.service >/dev/null 2>&1
nft -f "$NFT_FILE"
systemctl start awmpeer-egress.service

step "bashrc"
BASHRC="$HOME_DIR/.bashrc"
TMP_RC=$(mktemp)
{
    [ -n "$CRED" ] && echo "export AWM_PEER_CRED=$CRED_DIR/peer_cred"
    cat /etc/skel/.bashrc
} > "$TMP_RC"
install -m 644 -o "$PEER" -g "$PEER" "$TMP_RC" "$BASHRC"
rm -f "$TMP_RC"

if [ -n "$CRED" ]; then
    step "peer credential mirror"
    install -d -m 750 -o root -g "$PEER" "$CRED_DIR"
    cat > "$CRED_SVC" <<EOF
[Unit]
Description=Mirror the awm peer credential for $PEER

[Service]
Type=oneshot
ExecStart=/usr/bin/install -m 640 -o root -g $PEER $CRED $CRED_DIR/peer_cred
EOF
    cat > "$CRED_PATH" <<EOF
[Unit]
Description=Watch the awm peer credential for $PEER

[Path]
PathChanged=$CRED
Unit=awmpeer-cred.service

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now awmpeer-cred.path >/dev/null 2>&1
    systemctl start awmpeer-cred.service
fi

step "done"
nft list table inet "$PEER" | grep -q "skuid $PEER_UID" && echo "egress rule live for uid $PEER_UID"
sshd -T -C "user=$PEER,host=peer,addr=127.0.0.1" | grep -E '^(disableforwarding|allowtcpforwarding|allowagentforwarding|permittunnel) '
