#!/bin/sh
# Move this node's awm checkout onto the release commit a deploy promoted
# ($AWM_REFRESH_TO). The deploy's refresh step runs it from the checkout root
# (deploy.toml [refresh] sync), before the supervised restart.
#
# The commit comes from the checkout's own repo when a promote or a push already
# put it there, else from the `awm` remote: its release branch, or the
# refs/<node>/release a canonical node pushed. The checkout never moves off a
# commit of its own: a local commit fails the refresh for a session to look at.
#
# The body is one function, called on the last line. The reset rewrites this
# file while sh reads it, and sh reads a script lazily by byte offset, so an
# unwrapped body would run a splice of the old file and the new one.
main() {
    set -eu
    to=${AWM_REFRESH_TO:?AWM_REFRESH_TO names the release commit}
    remote=${AWM_REFRESH_REMOTE:-awm}

    if ! git cat-file -e "$to^{commit}" 2>/dev/null; then
        git fetch -q --no-tags "$remote" '+refs/*/release:refs/remotes/'"$remote"'/release-from/*'
    fi
    git cat-file -e "$to^{commit}" || { echo "release $to is not on $remote" >&2; exit 1; }

    head=$(git rev-parse HEAD)
    if ! git merge-base --is-ancestor "$head" "$to"; then
        echo "the checkout is at $head, which release $to does not contain. Land that work through deploy, then refresh again." >&2
        exit 1
    fi
    git reset -q --hard "$to"
    # Never -x: .awm/ is ignored and holds every service's state and credentials.
    # Without -fd, files release deleted linger, and a stale .py stays importable.
    git clean -fdq
}
main "$@"
