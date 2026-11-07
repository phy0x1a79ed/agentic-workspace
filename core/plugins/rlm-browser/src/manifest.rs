//! The verbs' descriptions and params, as the Python manifest published them.

pub struct VerbSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub params: &'static str,
}

pub const VERBS: &[VerbSpec] = &[
    VerbSpec { name: "acquire", description: "Acquire a browser realm session for a game; returns {session_id, mode, backend}. mode (default 'headless') is one of: 'headless' (launch Chrome, no display — fast, for bots at scale); 'rendered' (launch headful + VNC — watchable live); 'attached' (do NOT launch — drive an EXISTING real Chrome over CDP, e.g. the desktop browser; borrowed, so release never closes it, and only one attached session may be active at a time). mode is fixed for the session's life.", params: r#"[{"name": "game", "type": "string", "required": true}, {"name": "mode", "type": "string", "required": false}, {"name": "opts", "type": "object", "required": false}]"# },
    VerbSpec { name: "release", description: "Release a session: tear its Chrome down and drop the row.", params: r#"[{"name": "session_id", "type": "string", "required": true}]"# },
    VerbSpec { name: "reset", description: "Reset a session in place: close every tab, keep the browser + slot, open one fresh blank tab.", params: r#"[{"name": "session_id", "type": "string", "required": true}]"# },
    VerbSpec { name: "status", description: "Status of one session (pass session_id) or all (omit it). Returns {sessions: [{session_id, game, mode, backend, egress, tabs, ...}]}; egress is the inert VPN seam.", params: r#"[{"name": "session_id", "type": "string", "required": false}]"# },
    VerbSpec { name: "tab_open", description: "Open a new tab (optionally at url). Returns {tab_id}.", params: r#"[{"name": "session_id", "type": "string", "required": true}, {"name": "url", "type": "string", "required": false}]"# },
    VerbSpec { name: "tab_close", description: "Close a tab by tab_id.", params: r#"[{"name": "session_id", "type": "string", "required": true}, {"name": "tab_id", "type": "string", "required": true}]"# },
    VerbSpec { name: "tab_list", description: "List the session's live tabs. Returns {tabs: [...]}.", params: r#"[{"name": "session_id", "type": "string", "required": true}]"# },
    VerbSpec { name: "tab_activate", description: "Bring a tab to the foreground by tab_id.", params: r#"[{"name": "session_id", "type": "string", "required": true}, {"name": "tab_id", "type": "string", "required": true}]"# },
    VerbSpec { name: "observe", description: "Read a tab's RENDERED page (post-JS DOM). Returns {tab_id, html, url, title}. Omit tab_id for the active page.", params: r#"[{"name": "session_id", "type": "string", "required": true}, {"name": "tab_id", "type": "string", "required": false}]"# },
    VerbSpec { name: "screenshot", description: "Capture a PNG of a tab. Returns {tab_id, image (base64), format}. full_page=true captures beyond the viewport.", params: r#"[{"name": "session_id", "type": "string", "required": true}, {"name": "tab_id", "type": "string", "required": false}, {"name": "full_page", "type": "boolean", "required": false}]"# },
    VerbSpec { name: "cdp", description: "Relay an arbitrary Chrome DevTools Protocol method to the engine and return {result}. This single verb IS the full act surface (e.g. method='Page.navigate' params={url}; method='Runtime.evaluate' params={expression,returnByValue}). Routed to the tab when tab_id is given, else a default page; pass tab_id='__browser__' for browser-level methods. Discover available methods via the 'commands' verb.", params: r#"[{"name": "session_id", "type": "string", "required": true}, {"name": "method", "type": "string", "required": true}, {"name": "tab_id", "type": "string", "required": false}, {"name": "params", "type": "object", "required": false}]"# },
    VerbSpec { name: "commands", description: "Return the running Chrome's LIVE CDP protocol descriptor (/json/protocol): every domain/method/param available through the 'cdp' relay. Pass session_id to read that session's engine, or omit it for a throwaway probe browser. This is the dynamic, discoverable command catalog — not a hardcoded copy.", params: r#"[{"name": "session_id", "type": "string", "required": false}]"# },
];

pub const EMIT_TOPIC: &str = "browser";
pub const EMIT_DESCRIPTION: &str = "Fires on a realm-side browser event. Payload {session_id, tab_id, kind, data} where kind is e.g. 'page_loaded', 'dialog', or 'crashed' — projected as rlm.browser.<kind>. Effectors subscribe filtered to their session_id. Best-effort live signalling, not durable delivery.";
