/* AUTO-GENERATED — do not edit by hand.
 *
 * Regenerate with: npm run gen-types
 *
 * Source: `app.openapi()` from awm/exposed.py, called in-process via the
 * `awm` mamba env. The generator does not hit a running uvicorn.
 *
 * Engine `CONFIG_SCHEMA` JSON Schemas escape this pipeline: FastAPI types
 * the response envelope's inner `schema` field as `dict[str, Any]`, which
 * openapi-typescript emits as `unknown` / `Record<string, unknown>`.
 * Fixtures for engine forms must hand-shape JSON Schema blobs.
 */

export interface paths {
    "/rooms": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Rooms */
        get: operations["list_rooms_rooms_get"];
        put?: never;
        /** Create Room */
        post: operations["create_room_rooms_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/search": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Search Rooms */
        get: operations["search_rooms_rooms_search_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Room */
        get: operations["get_room_rooms__room_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/history": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get History */
        get: operations["get_history_rooms__room_id__history_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/posts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Post To Room */
        post: operations["post_to_room_rooms__room_id__posts_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/invite": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Invite To Room */
        post: operations["invite_to_room_rooms__room_id__invite_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/remove": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Remove From Room */
        post: operations["remove_from_room_rooms__room_id__remove_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/close": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Close Room */
        post: operations["close_room_rooms__room_id__close_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/archive": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Archive Room
         * @description Soft-archive a room. 409 with ``blocking_scopes`` if any agent
         *     (kind='scope') participants are still active.
         */
        post: operations["archive_room_rooms__room_id__archive_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Room Agents
         * @description Per-agent panel data — list scope/shadow_peer participants and,
         *     for local scopes, attach their agent-instance state from
         *     ``agent_instances._by_scope``.
         */
        get: operations["get_room_agents_rooms__room_id__agents_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/agents/{scope}/slash-commands": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Agent Slash Commands
         * @description Catalog of slash commands for the given scope.
         *
         *     Returns server-controlled commands plus any commands the scope's live
         *     claude reported in its init event (claude-specific list).
         */
        get: operations["get_agent_slash_commands_rooms__room_id__agents__scope__slash_commands_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/rooms/{room_id}/agents/{scope}/slash": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Post Agent Slash
         * @description Run a slash command against a scope's agent.
         *
         *     If the leading token matches a server command, the registered handler
         *     runs and its message is posted to the room as ``system``. Otherwise
         *     the line is forwarded unframed to the scope's claude stdin so /clear,
         *     /compact, plugin commands etc. still work.
         *
         *     Either way the original command is recorded in the room transcript as
         *     kind=``slash`` (authored by the caller) for audit.
         */
        post: operations["post_agent_slash_rooms__room_id__agents__scope__slash_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/inbox": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Peer Inbox
         * @description Receive a federated message from a remote peer.
         *
         *     The remote awm forwards a ``MessageSendRequest`` body here; the local
         *     inbox stores it after rewriting the sender to include the origin
         *     peer_id (extracted from ``X-Awm-From``).
         */
        post: operations["peer_inbox_peer_inbox_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/rooms/{room_id}/posts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Peer Room Post
         * @description Receive a post from a remote peer.
         *
         *     The author is rewritten to ``user:operator@<from_peer>`` so the
         *     transcript on this host stays unambiguous.
         */
        post: operations["peer_room_post_peer_rooms__room_id__posts_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/rooms/{room_id}/shadow-join": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Peer Shadow Join
         * @description A remote peer is announcing itself as a ``shadow_peer`` participant.
         */
        post: operations["peer_shadow_join_peer_rooms__room_id__shadow_join_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/rooms/{room_id}/shadow-leave": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Peer Shadow Leave */
        post: operations["peer_shadow_leave_peer_rooms__room_id__shadow_leave_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/rooms/agent-input": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Peer Agent Input
         * @description Remote peer is pushing an input frame for a scope that lives here.
         */
        post: operations["peer_agent_input_peer_rooms_agent_input_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/artifacts/{legacy_id}/content": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Peer Artifact Content
         * @description Stream the bytes of a local artifact for a remote peer that holds
         *     the metadata via replication but not the file. Only serves rows whose
         *     ``origin_peer`` is this peer — never re-federates.
         */
        get: operations["peer_artifact_content_peer_artifacts__legacy_id__content_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/lock/acquire": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Peer Lock Acquire
         * @description A remote peer is acquiring a lock on a resource that lives here.
         *     We never re-federate: the resource is presumed to live locally (the
         *     caller already resolved origin_peer to this peer).
         */
        post: operations["peer_lock_acquire_peer_lock_acquire_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/lock/release": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Peer Lock Release */
        post: operations["peer_lock_release_peer_lock_release_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/lock/heartbeat": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Peer Lock Heartbeat */
        post: operations["peer_lock_heartbeat_peer_lock_heartbeat_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/peer/db-sync": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Db Sync
         * @description Return cr-sqlite changes from ``crsql_changes`` strictly after
         *     ``since``. Response shape::
         *
         *         {
         *             "site_id":   "<b64 of this peer's crsql_site_id>",
         *             "db_version": <int — max db_version in this batch>,
         *             "changes": [
         *                 {"table": "rooms", "pk": {"_b":...}, "cid":"...",
         *                  "val":..., "col_version":N, "db_version":N,
         *                  "site_id":{"_b":...}, "cl":N, "seq":N},
         *                 ...
         *             ],
         *             "more": bool,
         *         }
         */
        get: operations["get_db_sync_peer_db_sync_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/stt": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Stt Http
         * @description Transcribe a raw int16 LE 16 kHz mono PCM body to text.
         *
         *     Fallback when the client can't keep the WebSocket open (e.g. while
         *     backgrounded). For the active foreground flow the WS path delivers
         *     the same payload via ``stt_result`` so other tabs see it too.
         */
        post: operations["stt_http_voice_stt_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/engines": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Engines List
         * @description `{kind: {engine_id: {schema, defaults}}}` for dynamic UI forms.
         */
        get: operations["engines_list_voice_engines_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/engines/loaded": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Engines Loaded */
        get: operations["engines_loaded_voice_engines_loaded_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/engines/{kind}/load": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Engines Load */
        post: operations["engines_load_voice_engines__kind__load_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/engines/{kind}/unload": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Engines Unload */
        post: operations["engines_unload_voice_engines__kind__unload_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/engines/global": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Engines Global Get */
        get: operations["engines_global_get_voice_engines_global_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/engines/{kind}/global": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Engines Global Put */
        put: operations["engines_global_put_voice_engines__kind__global_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/voice/tts/speak": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Tts Speak
         * @description Synthesize ``text`` via the currently loaded TTS engine.
         *
         *     Returns ``audio/wav`` so the browser ``Audio`` element plays it directly.
         *     Requires ``POST /voice/engines/tts/load`` first; 409 otherwise.
         */
        post: operations["tts_speak_voice_tts_speak_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/vagrant/session": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Session */
        post: operations["session_vagrant_session_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/hub/register": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Register */
        post: operations["register_hub_register_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/hub/services": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Services */
        get: operations["list_services_hub_services_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/hub/stripes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Stripes
         * @description Global stripe map. Unauthenticated by design — the dev-shell (or
         *     any composer) fetches this on mount to discover backend URLs to
         *     inject into stripe components via context. The actual backend
         *     requests still flow through the hub's authenticated proxy at
         *     ``<prefix>/_api/*``.
         */
        get: operations["list_stripes_hub_stripes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/hub/services/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Deregister */
        delete: operations["deregister_hub_services__name__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/mint": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Auth Mint
         * @description Mint a one-shot login challenge.
         *
         *     Loopback-only. Body: ``{"awm_user": "<name>"}``. Returns
         *     ``{"nonce": "...", "url": "https://.../auth/bootstrap?ot=..."}``. The
         *     operator opens the URL in a browser; ``/auth/bootstrap`` consumes the
         *     nonce and sets the bearer as an HttpOnly cookie.
         */
        post: operations["auth_mint_auth_mint_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/bootstrap": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Auth Bootstrap
         * @description Consume a one-shot nonce and set the bearer cookie.
         *
         *     The bearer itself is stored verbatim in the HttpOnly ``awm_session``
         *     cookie — no server-side session state. A non-HttpOnly ``awm_as``
         *     cookie carries the operator's display name so the SPA can populate
         *     ``X-Awm-As``.
         */
        get: operations["auth_bootstrap_auth_bootstrap_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/whoami": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Auth Whoami
         * @description Return the operator display name (from ``awm_as`` cookie).
         *
         *     Used by ``/ui/login.html`` to poll for completion after the operator
         *     clicks a DM'd bootstrap link in another tab.
         */
        get: operations["auth_whoami_auth_whoami_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/auth/session": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /**
         * Auth Logout
         * @description Clear the bearer cookie. Operator must re-login via Discord or CLI.
         */
        delete: operations["auth_logout_auth_session_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/ui/{full_path}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Spa */
        get: operations["_spa_ui__full_path__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/ui/": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Spa */
        get: operations["_spa_ui__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/ui": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Spa */
        get: operations["_spa_ui_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** AgentSlashCatalog */
        AgentSlashCatalog: {
            /** Server */
            server: components["schemas"]["SlashCommandInfo"][];
            /** Claude */
            claude: string[];
        };
        /** AgentSlashRequest */
        AgentSlashRequest: {
            /** Cmd */
            cmd: string;
        };
        /** AgentSlashResponse */
        AgentSlashResponse: {
            /** Handled */
            handled: boolean;
            /** Result */
            result: string;
        };
        /** EngineGlobalRequest */
        EngineGlobalRequest: {
            /** Engine Id */
            engine_id: string;
            /** Params */
            params?: {
                [key: string]: unknown;
            };
        };
        /** EngineLoadRequest */
        EngineLoadRequest: {
            /** Id */
            id: string;
            /** Params */
            params?: {
                [key: string]: unknown;
            };
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /** LiveAgentState */
        LiveAgentState: {
            /** Pid */
            pid?: number | null;
            /** Status */
            status?: string | null;
            /** Started At */
            started_at?: string | null;
            /** Exited At */
            exited_at?: string | null;
            /** Exit Code */
            exit_code?: number | null;
            /** Agent Cli */
            agent_cli?: string | null;
            /** Permission Mode */
            permission_mode?: string | null;
            /** Model */
            model?: string | null;
            /** Effort */
            effort?: string | null;
            /** Claude Session Id */
            claude_session_id?: string | null;
            /** Context Used */
            context_used?: number | null;
            /** Context Max */
            context_max?: number | null;
        };
        /** ParticipantInfo */
        ParticipantInfo: {
            /** Room Id */
            room_id: string;
            /** Kind */
            kind: string;
            /** Identifier */
            identifier: string;
            /** Joined At */
            joined_at: string;
            /** Left At */
            left_at?: string | null;
        };
        /** PostInfo */
        PostInfo: {
            /** Id */
            id: number;
            /** Room Id */
            room_id: string;
            /** Author */
            author: string;
            /** Body */
            body: string;
            /** Kind */
            kind: string;
            /** Ts */
            ts: string;
        };
        /** RegisterRequest */
        RegisterRequest: {
            /** Name */
            name: string;
            /** Prefix */
            prefix: string;
            /**
             * Url
             * @description Local URL to forward requests to (kind=url). Mutually exclusive with `static` and `stripe`.
             */
            url?: string | null;
            /** @description Directory-serving spec (kind=static). Mutually exclusive with `url` and `stripe`. */
            static?: components["schemas"]["StaticSpec"] | null;
            /** @description Component-and-backend bundle (kind=stripe). Mutually exclusive with `url` and `static`. */
            stripe?: components["schemas"]["StripeSpec"] | null;
        };
        /** RegisterResponse */
        RegisterResponse: {
            /** Service Id */
            service_id: string;
            /** Name */
            name: string;
            /** Prefix */
            prefix: string;
            /** Kind */
            kind: string;
            /** Url */
            url?: string | null;
            static?: components["schemas"]["StaticSpec"] | null;
            stripe?: components["schemas"]["StripeSpec"] | null;
            /** Lease Ws Path */
            lease_ws_path: string;
        };
        /** RoomActionResponse */
        RoomActionResponse: {
            /** Message */
            message: string;
            room?: components["schemas"]["RoomInfo"] | null;
            post?: components["schemas"]["PostInfo"] | null;
            participant?: components["schemas"]["ParticipantInfo"] | null;
        };
        /** RoomAgentInfo */
        RoomAgentInfo: {
            /** Scope */
            scope: string;
            /** Kind */
            kind: string;
            /** Identifier */
            identifier: string;
            /** Joined At */
            joined_at: string;
            live?: components["schemas"]["LiveAgentState"] | null;
        };
        /** RoomAgentsResponse */
        RoomAgentsResponse: {
            /** Agents */
            agents: components["schemas"]["RoomAgentInfo"][];
        };
        /**
         * RoomArchiveBlockedResponse
         * @description Body for the 409 returned when ``POST /rooms/{id}/archive`` is
         *     refused due to remaining active scope participants.
         */
        RoomArchiveBlockedResponse: {
            /**
             * Error
             * @default room_archive_blocked
             */
            error: string;
            /** Room Id */
            room_id: string;
            /** Blocking Scopes */
            blocking_scopes: string[];
        };
        /** RoomCloseRequest */
        RoomCloseRequest: {
            /**
             * Kill Agents
             * @default false
             */
            kill_agents: boolean;
        };
        /** RoomCreateRequest */
        RoomCreateRequest: {
            /** Topic */
            topic?: string | null;
            /** Scopes */
            scopes?: string[];
            /** Prompts */
            prompts?: {
                [key: string]: string;
            };
            /**
             * Close On Exit
             * @default false
             */
            close_on_exit: boolean;
        };
        /** RoomDetail */
        RoomDetail: {
            room: components["schemas"]["RoomInfo"];
            /** Participants */
            participants: components["schemas"]["ParticipantInfo"][];
            /** Recent */
            recent: components["schemas"]["PostInfo"][];
        };
        /** RoomHistoryResponse */
        RoomHistoryResponse: {
            /** Posts */
            posts: components["schemas"]["PostInfo"][];
            /** Total */
            total: number;
        };
        /** RoomInfo */
        RoomInfo: {
            /** Id */
            id: string;
            /** Host Peer Id */
            host_peer_id: string;
            /** Created At */
            created_at: string;
            /** Closed At */
            closed_at?: string | null;
            /** Topic */
            topic?: string | null;
            /** Status */
            status: string;
            /**
             * Close On Exit
             * @default false
             */
            close_on_exit: boolean;
        };
        /** RoomInviteRequest */
        RoomInviteRequest: {
            /** Scope */
            scope: string;
            /** Prompt */
            prompt?: string | null;
        };
        /** RoomListResponse */
        RoomListResponse: {
            /** Rooms */
            rooms: components["schemas"]["RoomInfo"][];
            /** Total */
            total: number;
        };
        /** RoomPostRequest */
        RoomPostRequest: {
            /** Body */
            body: string;
            /**
             * Kind
             * @default text
             */
            kind: string;
            /** To */
            to?: string | null;
        };
        /** RoomRemoveRequest */
        RoomRemoveRequest: {
            /** Scope */
            scope: string;
        };
        /** SlashCommandInfo */
        SlashCommandInfo: {
            /** Name */
            name: string;
            /** Args */
            args: string;
            /** Description */
            description: string;
        };
        /** StaticSpec */
        StaticSpec: {
            /**
             * Dir
             * @description Absolute path on the hub host to serve at the prefix.
             */
            dir: string;
            /**
             * Entry
             * @description Relative path to the ESM entry script. When the dir has no index.html, the hub renders a minimal shell that loads this script as a module.
             */
            entry?: string | null;
            /**
             * Css
             * @description Optional CSS files (relative to dir) injected into the auto-shell.
             */
            css?: string[];
            /**
             * Mount Id
             * @description DOM id of the mount node in the auto-shell.
             * @default app
             */
            mount_id: string;
        };
        /** StripeBackend */
        StripeBackend: {
            /**
             * Cmd
             * @description argv-style command to spawn the backend. ${AWM_SERVICE_PORT} in any arg is substituted with the hub-allocated port before exec.
             */
            cmd: string[];
            /**
             * Env
             * @description Extra env vars merged into the process env. AWM_SERVICE_PORT is hub-injected and wins on collision.
             */
            env?: {
                [key: string]: string;
            };
            /**
             * Health
             * @description Path on the backend the hub polls until 200 to mark ready.
             * @default /healthz
             */
            health: string;
            /**
             * Cwd
             * @description Working directory for the spawned process. Defaults to the static dir.
             */
            cwd?: string | null;
        };
        /** StripeSpec */
        StripeSpec: {
            /**
             * Dir
             * @description Absolute path to the frontend bundle dir (canonical-paths static serving at the prefix root).
             */
            dir: string;
            /** @description Optional supervised backend. When set, the hub spawns it at register time and proxies <prefix>/_api/* to it. */
            backend?: components["schemas"]["StripeBackend"] | null;
        };
        /** TtsSpeakRequest */
        TtsSpeakRequest: {
            /** Text */
            text: string;
        };
        /** VagrantSessionResponse */
        VagrantSessionResponse: {
            /** Scope Uuid */
            scope_uuid: string;
            /** Room Id */
            room_id: string;
            /** Scope Identifier */
            scope_identifier: string;
            /** Manager Live */
            manager_live: boolean;
        };
        /** ValidationError */
        ValidationError: {
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
            /** Input */
            input?: unknown;
            /** Context */
            ctx?: Record<string, never>;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    list_rooms_rooms_get: {
        parameters: {
            query?: {
                status?: string;
                participating_scope?: string | null;
                peer?: string | null;
                limit?: number;
            };
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomListResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_room_rooms_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RoomCreateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomInfo"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    search_rooms_rooms_search_get: {
        parameters: {
            query: {
                q: string;
                peer?: string | null;
                limit?: number;
            };
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_room_rooms__room_id__get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomDetail"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_history_rooms__room_id__history_get: {
        parameters: {
            query?: {
                before_ts?: string | null;
                limit_chars?: number;
            };
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomHistoryResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_to_room_rooms__room_id__posts_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RoomPostRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomActionResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    invite_to_room_rooms__room_id__invite_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RoomInviteRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomActionResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    remove_from_room_rooms__room_id__remove_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RoomRemoveRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomActionResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    close_room_rooms__room_id__close_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RoomCloseRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomActionResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    archive_room_rooms__room_id__archive_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomActionResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomArchiveBlockedResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_room_agents_rooms__room_id__agents_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoomAgentsResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_slash_commands_rooms__room_id__agents__scope__slash_commands_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
                scope: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentSlashCatalog"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_agent_slash_rooms__room_id__agents__scope__slash_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
                scope: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentSlashRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentSlashResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_inbox_peer_inbox_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_room_post_peer_rooms__room_id__posts_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_shadow_join_peer_rooms__room_id__shadow_join_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_shadow_leave_peer_rooms__room_id__shadow_leave_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                room_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_agent_input_peer_rooms_agent_input_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_artifact_content_peer_artifacts__legacy_id__content_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                legacy_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_lock_acquire_peer_lock_acquire_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_lock_release_peer_lock_release_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    peer_lock_heartbeat_peer_lock_heartbeat_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_db_sync_peer_db_sync_get: {
        parameters: {
            query?: {
                /** @description cr-sqlite db_version cursor */
                since?: number;
                limit?: number;
            };
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    stt_http_voice_stt_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    engines_list_voice_engines_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    engines_loaded_voice_engines_loaded_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    engines_load_voice_engines__kind__load_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                kind: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EngineLoadRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    engines_unload_voice_engines__kind__unload_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                kind: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: boolean;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    engines_global_get_voice_engines_global_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    engines_global_put_voice_engines__kind__global_put: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                kind: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EngineGlobalRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    tts_speak_voice_tts_speak_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TtsSpeakRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    session_vagrant_session_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VagrantSessionResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    register_hub_register_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RegisterRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RegisterResponse"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_services_hub_services_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_stripes_hub_stripes_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    deregister_hub_services__name__delete: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    auth_mint_auth_mint_post: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    auth_bootstrap_auth_bootstrap_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    auth_whoami_auth_whoami_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    auth_logout_auth_session_delete: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    _spa_ui__full_path__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                full_path: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    _spa_ui__get: {
        parameters: {
            query?: {
                full_path?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    _spa_ui_get: {
        parameters: {
            query?: {
                full_path?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
