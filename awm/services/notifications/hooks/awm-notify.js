/**
 * OpenCode → awm-notifications producer plugin.
 *
 * Install: copy/symlink into ~/.config/opencode/plugin/awm-notify.js
 * (global config — project-local opencode config is ignored unless the dir is
 * a git repo, so global is the reliable home).
 *
 * Subscribes to the OpenCode event bus (SDK 1.17.x shapes) and POSTs
 * normalized events to the notifications service:
 *   session.idle        → turn_end   (last assistant message fetched inline)
 *   session.error       → error
 *   permission.updated  → notification (blocked on the user)
 *   message.updated     → user_prompt (role === "user" only)
 *   session.created     → session_start   (top-level sessions only)
 *   session.deleted     → session_end
 *
 * The awm MCP is registered globally in ~/.config/opencode/opencode.jsonc, so
 * every OpenCode session qualifies as "loads the awm MCP".
 *
 * Fire-and-forget: every POST error is swallowed — a down gateway must never
 * affect the session. AWM_NOTIFY_DISABLE=1 disables.
 */

const HUB = (process.env.AWM_HUB_URL || "http://127.0.0.1:7819").replace(/\/+$/, "");
const ENDPOINT = `${HUB}/svc/notifications/fn/report`;

/** @type {import("@opencode-ai/plugin").Plugin} */
export const AwmNotify = async ({ client, directory }) => {
  if (process.env.AWM_NOTIFY_DISABLE === "1") return {};

  // sessionID → directory (from created/updated infos); child session ids to skip.
  const dirs = new Map();
  const children = new Set();

  const post = (body) => {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 5000);
      fetch(ENDPOINT, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: ctl.signal,
      })
        .catch(() => {})
        .finally(() => clearTimeout(t));
    } catch {
      /* fire-and-forget */
    }
  };

  const report = (event, sessionID, extra = {}) =>
    post({
      harness: "opencode",
      event,
      session_id: sessionID,
      cwd: dirs.get(sessionID) || directory,
      ...extra,
    });

  const lastAssistantText = async (sessionID) => {
    try {
      const res = await client.session.messages({ path: { id: sessionID } });
      const msgs = res?.data || [];
      for (let i = msgs.length - 1; i >= 0; i--) {
        const { info, parts } = msgs[i] || {};
        if (info?.role !== "assistant") continue;
        const text = (parts || [])
          .filter((p) => p?.type === "text" && p.text && !p.synthetic)
          .map((p) => p.text)
          .join("\n")
          .trim();
        if (text) return text;
      }
    } catch {
      /* best-effort */
    }
    return null;
  };

  return {
    event: async ({ event }) => {
      try {
        const p = event?.properties || {};
        switch (event?.type) {
          case "session.created":
          case "session.updated": {
            const info = p.info || {};
            if (!info.id) return;
            if (info.parentID) {
              children.add(info.id); // subagent — never a user-attention source
              return;
            }
            if (info.directory) dirs.set(info.id, info.directory);
            if (event.type === "session.created")
              report("session_start", info.id, { title: info.title });
            return;
          }
          case "session.deleted": {
            const info = p.info || {};
            if (!info.id || info.parentID || children.has(info.id)) return;
            report("session_end", info.id);
            return;
          }
          case "session.idle": {
            const id = p.sessionID;
            if (!id || children.has(id)) return;
            const last = await lastAssistantText(id);
            report("turn_end", id, { last_message: last });
            return;
          }
          case "session.error": {
            const id = p.sessionID;
            if (!id || children.has(id)) return;
            const err = p.error || {};
            report("error", id, {
              message: err?.data?.message || err?.name || "session error",
            });
            return;
          }
          case "permission.updated": {
            const id = p.sessionID;
            if (!id || children.has(id)) return;
            report("notification", id, {
              message: p.title || `permission: ${p.type || "?"}`,
            });
            return;
          }
          case "message.updated": {
            const info = p.info || {};
            if (info.role !== "user") return;
            const id = info.sessionID;
            if (!id || children.has(id)) return;
            report("user_prompt", id);
            return;
          }
          default:
            return;
        }
      } catch {
        /* never let the plugin surface an error into the session */
      }
    },
  };
};
