# plugin-guards

## Purpose

This file holds the result of the review topic on how `install.md` R116 items 3 and 4 guard a plugin. It records what the redux-plugins lane asked, how today's pages work, and the decisions.

## As built

- Each node serves every page and every service on one HTTPS origin. httpsfront passes each request to the loopback gateway (`httpsfront/proxy.py:1-20`), and one login cookie covers the origin. Any page can call any tool as Tony.
- A manifest's `[[needs.tool]]` is checked only at install, for the tool's presence and major. No call is refused for being undeclared (plugin contract v1.9).
- The lane asked how to give each plugin's pages their own origin and how to contain a third-party plugin. Both guarded against code the fleet did not write, and all plugins are written in the fleet.

## Decisions

- 2026-11-04: Installing a plugin trusts it, after Tony reviews it at the commit approval. The page origin per plugin and the third-party container go (`install.md` R116 item 4). Tony: "If we just do installing is trusting and analyze the plugin beforehand, is that a problem? Plus, all of the plugins are handwritten by us."
- 2026-11-04: The manifest declares each tool and verb a plugin calls, and the gateway refuses every other call. A service is known by its token, and a page by its path (`install.md` R116 item 3). Tony: "Can we make plugins declare exactly what other endpoints or services they will need to hit on AWM at install time and block all others? This serves as like a validation check, and the block is the guard." and "Yes, tool and verb."
