# _to_delete

Legacy packages from the pre-redesign `packages/*/` layout that this scope
moved aside but did not remove. A follow-up scope (`cleanup-legacy-packages`)
owns deciding whether to delete or revive each one.

| Package | Why it's here | Original role |
|---|---|---|
| `bus` | `@awm/bus` was the cross-stripe pub/sub library. The new model uses service emitters for cross-package coordination; the in-browser shim is not yet replaced. Anything still importing `from '@awm/bus'` must either migrate to a service emitter or move into `_to_delete/` with this package. |
| `dev-shell` | Was the dev-time stripe browser at `/dev/`. The new model serves pages at `/ui/<name>`; a follow-up `pages/dev/` page (or equivalent) is the natural replacement, but lives outside the current scope. |
| `hello` | Smoke-test stripe. Useful as a legacy-stripe regression reference for as long as `kind="stripe"` still exists; deletion can follow once kind="stripe" itself is gone. |
| `ptt` | Original `@awm/ptt` `kind=stripe` package (backend + nested SvelteKit + components in one tree). Replaced by the three-way split: `packages/services/ptt/` (control-WS adapter), `packages/components/ptt-composer/` (PttComposer + tabs + chips), `packages/pages/ptt/` (demo page mounting `<PttComposer/>`). Parked here after migration; deletion is appropriate once the three replacements are signed off end-to-end and the legacy `awm stripe register --package packages/ptt` flow is removed from anyone's muscle memory. |

npm workspaces are scoped to `packages/{components,pages}/*` so anything
under `_to_delete/` is invisible to `npm install` and to `awm packages sync`
— these directories are inert.
