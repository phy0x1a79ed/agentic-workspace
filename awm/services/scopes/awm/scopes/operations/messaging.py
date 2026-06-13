"""Messaging operation definitions for the gateway manifest."""

from awm.scopes.models import MessageSendRequest
from awm.scopes import messaging


MESSAGING_MANIFEST_FUNCTIONS = [
    {
        "name": "inbox_send",
        "description": "Send a message to a scope's inbox.",
        "params": [
            {"name": "scope", "type": "string", "required": True},
            {"name": "sender", "type": "string", "required": True},
            {"name": "msg_type", "type": "string", "required": True},
            {"name": "subject", "type": "string", "required": True},
            {"name": "body", "type": "string", "required": True},
            {"name": "metadata", "type": "string", "required": False},
        ],
    },
    {
        "name": "inbox_search",
        "description": "Search messages by scope, status, type, or free-text query.",
        "params": [
            {"name": "scope", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": False},
            {"name": "msg_type", "type": "string", "required": False},
            {"name": "query", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
        ],
    },
    {
        "name": "inbox_fetch",
        "description": "Fetch messages for a scope, optionally marking them as read.",
        "params": [
            {"name": "scope", "type": "string", "required": True},
            {"name": "status", "type": "string", "required": False},
            {"name": "msg_type", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "mark_read", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "inbox_mark_read",
        "description": "Mark a single message as read by ID.",
        "params": [
            {"name": "message_id", "type": "integer", "required": True},
        ],
    },
    {
        "name": "inbox_recipients",
        "description": "List valid recipient scopes matching a query (or '*' for all).",
        "params": [
            {"name": "query", "type": "string", "required": True},
            {"name": "include_inactive", "type": "boolean", "required": False},
        ],
    },
]


def _handle_inbox_send(args: dict) -> dict:
    req = MessageSendRequest(
        scope=args["scope"],
        sender=args["sender"],
        msg_type=args["msg_type"],
        subject=args["subject"],
        body=args["body"],
        metadata=args.get("metadata"),
    )
    result = messaging.send_message(req)
    return result.model_dump()


def _handle_inbox_search(args: dict) -> dict:
    result = messaging.search_messages(
        scope=args.get("scope"),
        status=args.get("status"),
        msg_type=args.get("msg_type"),
        query=args.get("query"),
        limit=int(args.get("limit", 50)),
    )
    return result.model_dump()


def _handle_inbox_fetch(args: dict) -> dict:
    result = messaging.fetch_messages(
        scope=args["scope"],
        status=args.get("status"),
        msg_type=args.get("msg_type"),
        limit=int(args.get("limit", 50)),
        mark_read=bool(args.get("mark_read", False)),
    )
    return result.model_dump()


def _handle_inbox_mark_read(args: dict) -> dict:
    result = messaging.mark_read(args["message_id"])
    return result.model_dump()


def _handle_inbox_recipients(args: dict) -> dict:
    recipients = messaging.list_recipients(
        args["query"],
        include_inactive=bool(args.get("include_inactive", False)),
    )
    return {"recipients": recipients}


MESSAGING_HANDLERS = {
    "inbox_send": _handle_inbox_send,
    "inbox_search": _handle_inbox_search,
    "inbox_fetch": _handle_inbox_fetch,
    "inbox_mark_read": _handle_inbox_mark_read,
    "inbox_recipients": _handle_inbox_recipients,
}
