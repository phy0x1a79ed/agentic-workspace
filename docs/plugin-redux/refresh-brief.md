Behavior: (1) A batch keeps `gateway` and plugin names together in its restart list; node-refresh passes both (`--restart --reload-restart a,b`). (2) A routine without [fixer] posts its failure to the operator channel and starts no agent; every operator notice defaults to unimatrix0#notifications; the full-tests routine is gone. (3) node-refresh skips awm/install.sh unless an install input under awm/ or the node's config.toml changed.
Proof: routine tests 85 pass, config tests 16 pass; install_stamp is identical across recent commits and covers 79 install inputs.
Live check: on a node after the second refresh, the refresh log shows `install: no`, and the deploy step takes seconds. A failed routine shows role "notice" in `routine runs`.
Restart: none. The routine plugin and the config library are reloaded by the deploy's stamp anyway until hot-reload/reload lands.
Merger: each node's first refresh with this does one full install, to record its stamp.
