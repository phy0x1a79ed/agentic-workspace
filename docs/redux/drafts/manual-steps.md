# Manual steps

## Purpose & Contents

This file lists the steps of the redux build that only Tony can take, so he takes them in one sitting. One top-level agent of the build walks him through them in order. Each step names what it unblocks. A lane that finds a new step adds it here instead of asking Tony on its own. Tony: "The manual steps here needs to be grouped with the other manual steps that I'll tackle all at once." and "When we do the parallel implementation, I guess one of the top level agents will be walking me through all of the manual stuff that I need to do." (2026-11-04).

## Steps

1. Cloudflare DNS: point `auth.tony-xy-liu.com` at deneb's public address, as DNS only, not proxied, so deneb forwards its TLS unopened (`queue.md` R64).
2. Cloudflare DNS: add the CAA record `0 issue "letsencrypt.org; validationmethods=dns-01"` for `auth.tony-xy-liu.com`.
3. Cloudflare: make an API token that edits only the `tony-xy-liu.com` DNS zone, for alcor's DNS-01 challenge. The walkthrough agent stores it as a secret on alcor and never prints it.
4. Enrol the passkey on the approver at `auth.tony-xy-liu.com`. This yields `approver_grant_pub` for the fleet values.
5. Give lyra an ssh key into alcor for recovery (`queue.md` R85), and admit it in alcor's firewall.
6. Approve the push of each branch to mizar and of `release` to GitHub (`install.md` R114), and the tether push and pin bump.
7. Confirm `goal.md`.
