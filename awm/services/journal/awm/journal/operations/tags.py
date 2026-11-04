"""Tag verbs for the gateway manifest (fleet.md R107 items 6 and 7).

``tag_add``, ``tag_edit`` and ``tag_list`` keep the vocabulary; ``retag`` adds
and removes several tags on one post. ``tag_receive`` takes a peer's pushed
tags (:mod:`awm.journal.tagsync`).
"""

from __future__ import annotations

from awm.journal import channel, private, tags, tagsync

_FOREIGN = "the tag vocabulary is kept by this node and its own peers only"

TAG_MANIFEST_FUNCTIONS = [
    {
        "name": "tag_add",
        "tool": "journal_tag_add",
        "effect": "write",
        "description": (
            "Add a tag to the vocabulary that every node shares. Check tag_list first: add "
            "only a concept no tag covers. The description is required and says what a "
            "post with this tag is. The add reaches every node by itself."
        ),
        "params": [
            {"name": "name", "type": "string", "required": True,
             "description": "Lowercase letters, digits and _ . : + / -, at most 64 characters."},
            {"name": "description", "type": "string", "required": True},
        ],
    },
    {
        "name": "tag_edit",
        "tool": "journal_tag_edit",
        "effect": "write",
        "description": (
            "Change a tag's name or description. Posts link a tag by its id, so a rename "
            "shows on every post. The earlier versions stay; tag_list with tag shows them."
        ),
        "params": [
            {"name": "tag", "type": "string", "required": True,
             "description": "The tag's id or current name."},
            {"name": "name", "type": "string", "required": False},
            {"name": "description", "type": "string", "required": False},
        ],
    },
    {
        "name": "tag_list",
        "tool": "journal_tag_list",
        "effect": "read",
        "description": (
            "The tag vocabulary: each tag with its id and description, and any open "
            "clash between two nodes that added one name. With tag, that tag's versions."
        ),
        "params": [
            {"name": "tag", "type": "string", "required": False,
             "description": "A tag's id or name, to list its earlier versions."},
        ],
    },
    {
        "name": "retag",
        "tool": "journal_retag",
        "effect": "write",
        "description": (
            "Add and remove several tags on one post at once, such as add=[closed]. "
            "A type tag (message, journal, goal, brief, precedent) is set only when the "
            "post is made; closed and superseded change freely."
        ),
        "params": [
            {"name": "post_id", "type": "string", "required": True},
            {"name": "add", "type": "array", "required": False},
            {"name": "remove", "type": "array", "required": False},
        ],
    },
    {
        "name": "tag_receive",
        "tool": "journal_tag_receive",
        "effect": "write",
        "surfaces": ["http"],
        "description": "A peer node's pushed tags, merged into this node's vocabulary.",
        "params": [{"name": "tags", "type": "array", "required": True}],
    },
]


def _local_only(as_: str | None) -> None:
    if channel.is_foreign(as_):
        raise PermissionError(_FOREIGN)


def _handle_tag_add(args: dict, as_: str | None = None) -> dict:
    _local_only(as_)
    return {"tag": tags.add(args["name"], args.get("description") or "",
                            by=as_ or "").to_dict()}


def _handle_tag_edit(args: dict, as_: str | None = None) -> dict:
    _local_only(as_)
    return {"tag": tags.edit(args["tag"], name=args.get("name"),
                             description=args.get("description"), by=as_ or "").to_dict()}


def _handle_tag_list(args: dict, as_: str | None = None) -> dict:
    if args.get("tag"):
        return {"tag": args["tag"], "versions": tags.versions(args["tag"])}
    vocab = [t.to_dict() for t in tags.vocabulary()]
    return {"tags": vocab, "total": len(vocab), "clashes": tagsync.clashes()}


def _handle_retag(args: dict, as_: str | None = None) -> dict:
    _local_only(as_)
    post = channel.get_post(args["post_id"])
    if post is None or private.hides_post(as_, post):
        raise ValueError(f"no post {args['post_id']}")
    return {"post": channel.set_tags(post.id, add=args.get("add"),
                                     remove=args.get("remove")).to_dict()}


def _handle_tag_receive(args: dict, as_: str | None = None) -> dict:
    return tagsync.receive(args.get("tags"), as_)


TAG_HANDLERS = {
    "tag_add": _handle_tag_add,
    "tag_edit": _handle_tag_edit,
    "tag_list": _handle_tag_list,
    "retag": _handle_retag,
    "tag_receive": _handle_tag_receive,
}
