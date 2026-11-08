# Port plans

## Purpose & Contents

This folder holds one port plan per plugin or shared crate of the plugin redux, plus the `describe` baselines the ports are checked against. A plan names only what the Python does not make obvious: the byte formats and behaviours to keep exactly, the deliberate changes, and the tests. The Python at release 49232bf9 is the specification for everything else.

- `<name>.md`: the port plan for a plugin or a shared crate.
- `baseline/<tool>.describe.json`: each tool's `describe` at release 49232bf9. A port's describe must equal it, apart from the changes its plan lists.

Delete a plan once its port has landed and its crate's own README carries what still matters.
