# nexus-signin: the passkey sign-in on nexus is refused

> https://nexus.tony-xy-liu.com/ {"error":"the sign-in grant is refused"} after passkey success, im on capella

## Cause

deneb serves nexus. Its httpsfront logged `portal grant refused: no AWM_APPROVER_GRANT_PUBKEY on this node`. Without alcor's grant public key it cannot verify any sign-in grant, so every passkey sign-in failed after the passkey step.

deneb's `config.toml` was rendered at 03:29Z on 2026-11-06. alcor's grant key reached cloud dev 11 minutes later (cloud ce655e8, 03:40Z, `group_vars/all/approver.yml`). No run rendered deneb's config again after that.

## Fix (2026-11-06, about 17:45Z)

- Ran `site.yml --tags awm_config --limit deneb` from cloud dev (worktree `cloud/redux-dev-apply`). It added the key, `lyra` in `[fleet] nodes`, and edge-list updates (rtmac, shogun's tailnet address).
- Restarted deneb's gateway unit. All 7 services came back ready, and the httpsfront process holds the key.
- Check: a bogus grant is now refused as `the grant is not base64url JSON`, no longer for a missing key.

## Other nodes

alcor, altair and mizar already have the key. capella and shogun have none in `config.toml`. The fleet-endstate worker (daae3825) renders awm_config on both.

## Follow-up for awm

The gateway reads `config.toml [env]` only when it starts. `awm reload` and `awm restart --name <svc>` do not pass a changed `[env]` to the plugins, and nothing reports that the change is pending. An `[env]` change needs a gateway restart.
