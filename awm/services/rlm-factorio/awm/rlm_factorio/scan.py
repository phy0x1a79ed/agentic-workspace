"""Paginated entity survey: many cheap engine commands instead of one huge one.

A scan walks the area's 32x32 chunks in a fixed row-major order. Each entity
belongs to the chunk holding its position, which dedupes an entity whose box
spans several chunks. One engine command stops at the result limit or at a
work cap counted in entities examined -- Lua has no clock, so a count is the
only deterministic stop. The cap is checked after every emitted entity, so a
dense chunk splits across pages too, and every page emits at least one entity.
The realm resizes that cap from each command's
measured cost, so a dense area costs more commands, never a slow tick.

The cursor carries the resolved area and a position inside it (chunk index,
owned entities to skip). Pages are not a snapshot: an entity built or removed
between pages can be missed or seen twice.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Callable

from awm.rlm_factorio import throttle

DEFAULT_LIMIT = 200
MAX_LIMIT = 5000
DEFAULT_FIELDS = ("name", "position")
FIELDS = ("name", "type", "position", "direction", "status", "recipe",
          "unit_number", "health", "ghost_name", "amount", "contents", "force")
WALL_S = 30.0
CAP_START, CAP_MIN, CAP_MAX = 300, 50, 50_000
TARGET_SHARE = 0.4      # aim each page at this share of the command budget

PAGE_LUA = r"""local h = helpers
local P = h.json_to_table('@PARAMS@')
local s = game.surfaces[P.surface]
if not s then error("no such surface: " .. tostring(P.surface)) end
local floor, max, min = math.floor, math.max, math.min
local cx1, cy1 = floor(P.x1 / 32), floor(P.y1 / 32)
local cx2, cy2 = floor((P.x2 - 1e-9) / 32), floor((P.y2 - 1e-9) / 32)
local w = cx2 - cx1 + 1
local n = w * (cy2 - cy1 + 1)
@ROW@
local filt = {name = P.name, type = P.type, force = P.force}
local out, count, work, ci, skip = {}, 0, 0, P.ci, P.skip
local nxt, done = nil, false
while ci < n do
local cx, cy = cx1 + ci % w, cy1 + floor(ci / w)
local k = 0
if s.is_chunk_generated({cx, cy}) then
filt.area = {{max(P.x1, cx * 32), max(P.y1, cy * 32)},
{min(P.x2, cx * 32 + 32), min(P.y2, cy * 32 + 32)}}
local ents = s.find_entities_filtered(filt)
work = work + 1
for i = 1, #ents do
local e = ents[i]
work = work + 1
local p = e.position
if floor(p.x / 32) == cx and floor(p.y / 32) == cy and p.x >= P.x1
and p.x < P.x2 and p.y >= P.y1 and p.y < P.y2 then
k = k + 1
if k > skip then
count = count + 1
if not P.count_only then out[#out + 1] = row(e) end
if count >= P.limit or work >= P.work_cap then nxt = {ci, k}; done = true; break end
end
end
end
else
work = work + 1
end
if done then break end
ci, skip = ci + 1, 0
if work >= P.work_cap and ci < n then nxt = {ci, 0}; break end
end
rcon.print(h.table_to_json({rows = out, count = count, next = nxt, work = work, chunks = n}))"""

# One Lua statement per field, so a page carries only what was asked for: the
# command's text is replicated to every peer at about 90 bytes a tick, so every
# byte of it is latency.
ROW_LUA = {
    "name": "r.name=e.name",
    "type": "r.type=e.type",
    "position": "r.position=e.position",
    "direction": "r.direction=e.direction",
    "unit_number": "r.unit_number=e.unit_number",
    "force": "r.force=e.force.name",
    "health": "r.health=e.health",
    "status": "if e.status then r.status=SN[e.status] end",
    "ghost_name": 'if e.type=="entity-ghost" then r.ghost_name=e.ghost_name end',
    "amount": 'if e.type=="resource" then r.amount=e.amount end',
    "recipe": ('if e.type=="assembling-machine" or e.type=="furnace" then '
               'local ok,rc=pcall(e.get_recipe) rc=ok and rc or nil '
               'if not rc and e.type=="furnace" and e.previous_recipe then '
               'rc=e.previous_recipe.name end '
               'if rc then r.recipe=type(rc)=="string" and rc or rc.name end end'),
    "contents": ('local ok,top=pcall(e.get_max_inventory_index) if ok and top then '
                 'local c={} for i=1,top do local inv=e.get_inventory(i) if inv then '
                 'for _,it in pairs(inv.get_contents()) do '
                 'c[it.name]=(c[it.name] or 0)+it.count end end end '
                 'if next(c) then r.contents=c end end'),
}
STATUS_LUA = "local SN={} for k,v in pairs(defines.entity_status) do SN[v]=k end"


def _row_lua(fields) -> str:
    body = " ".join(ROW_LUA[f] for f in FIELDS if f in fields)
    head = STATUS_LUA + "\n" if "status" in fields else ""
    return f"{head}local function row(e) local r={{}} {body} return r end"


class ScanError(ValueError):
    pass


def _query_hash(q: dict) -> str:
    key = json.dumps({k: q.get(k) for k in ("surface", "name", "type", "force",
                                            "x1", "y1", "x2", "y2")}, sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def encode_cursor(q: dict, ci: int, skip: int) -> str:
    raw = json.dumps({"a": [q["x1"], q["y1"], q["x2"], q["y2"]], "s": q["surface"],
                      "c": ci, "k": skip, "h": _query_hash(q)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict:
    try:
        pad = "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(cursor + pad))
    except Exception as exc:  # noqa: BLE001
        raise ScanError(f"unreadable cursor: {exc}") from None


def resolve_area(args: dict, seat_position: Callable[[], dict] | None) -> dict:
    """The query: surface, filters and an explicit, ordered area."""
    q = {"surface": args.get("surface") or "nauvis", "name": args.get("name"),
         "type": args.get("type"), "force": args.get("force")}
    if args.get("cursor"):
        cur = decode_cursor(args["cursor"])
        q["x1"], q["y1"], q["x2"], q["y2"] = cur["a"]
        q["surface"] = cur["s"]
        if cur.get("h") != _query_hash(q):
            raise ScanError("cursor belongs to a scan with other filters; pass the "
                            "same name/type/force/surface as the first page")
        q["ci"], q["skip"] = int(cur["c"]), int(cur["k"])
        return q
    corners = [args.get(k) for k in ("x1", "y1", "x2", "y2")]
    if all(c is not None for c in corners):
        x1, y1, x2, y2 = corners
        q["x1"], q["x2"] = min(x1, x2), max(x1, x2)
        q["y1"], q["y2"] = min(y1, y2), max(y1, y2)
    elif args.get("radius") is not None:
        if args.get("x") is not None and args.get("y") is not None:
            cx, cy = args["x"], args["y"]
        elif seat_position is not None:
            pos = seat_position()
            cx, cy = pos["x"], pos["y"]
        else:
            raise ScanError("radius needs x,y or a seat to centre on")
        r = args["radius"]
        q["x1"], q["y1"], q["x2"], q["y2"] = cx - r, cy - r, cx + r, cy + r
    else:
        raise ScanError("scan needs an area: x1,y1,x2,y2, or radius (around x,y "
                        "or the seat)")
    if (q["x2"] - q["x1"]) * (q["y2"] - q["y1"]) > 8192 * 8192:
        raise ScanError("scan area is over 8192x8192 tiles; split it")
    q["ci"], q["skip"] = 0, 0
    return q


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=int)]
    return []


def page_code(q: dict, *, fields, limit: int, count_only: bool, work_cap: int) -> str:
    params = {k: q[k] for k in ("surface", "x1", "y1", "x2", "y2", "ci", "skip")}
    params.update({"limit": limit, "count_only": count_only, "work_cap": work_cap})
    for key in ("name", "type", "force"):
        if q.get(key) is not None:
            params[key] = q[key]
    blob = json.dumps(params, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")
    return (PAGE_LUA.replace("@ROW@", _row_lua(fields))
            .replace("@PARAMS@", blob))


class Scanner:
    """Runs scans for many keys, remembering a work cap per key."""

    def __init__(self):
        self.caps: dict[str, int] = {}

    def _adapt(self, key: str, cost_ms: float | None, work: int) -> None:
        # Cost per entity examined varies about tenfold with the fields asked
        # for, so scale the cap by measured cost, damped to at most 2x a page.
        if cost_ms is None or work <= 0:
            return
        cap = self.caps.get(key, CAP_START)
        fit = work * TARGET_SHARE * throttle.CMD_BUDGET_MS / max(cost_ms, 0.01)
        self.caps[key] = int(max(CAP_MIN, min(CAP_MAX, cap * 2, fit)))

    def scan(self, key: str, args: dict, run: Callable[[str], tuple[str, float | None]],
             seat_position: Callable[[], dict] | None = None,
             clock=time.monotonic) -> dict:
        q = resolve_area(args, seat_position)
        count_only = bool(args.get("count_only"))
        limit = int(args.get("limit") or DEFAULT_LIMIT)
        if not 1 <= limit <= MAX_LIMIT:
            raise ScanError(f"limit must be within [1, {MAX_LIMIT}]")
        fields = args.get("fields") or DEFAULT_FIELDS
        bad = [f for f in fields if f not in FIELDS]
        if bad:
            raise ScanError(f"unknown fields {bad}; choose from {', '.join(FIELDS)}")
        rows: list = []
        total = commands = 0
        cost_total = 0.0
        deadline = clock() + WALL_S
        while True:
            want = (10 ** 9) if count_only else limit - len(rows)
            out, cost = run(page_code(q, fields=fields, limit=want, count_only=count_only,
                                      work_cap=self.caps.get(key, CAP_START)))
            commands += 1
            cost_total += cost or 0.0
            try:
                page = json.loads(out)
            except ValueError:
                raise ScanError(out.strip() or "scan page returned nothing") from None
            self._adapt(key, cost, int(page.get("work") or 0))
            rows.extend(_as_list(page.get("rows")))
            total += int(page.get("count") or 0)
            nxt = _as_list(page.get("next")) or None
            if nxt is None:
                cursor = None
                break
            q["ci"], q["skip"] = int(nxt[0]), int(nxt[1])
            cursor = encode_cursor(q, q["ci"], q["skip"])
            if (not count_only and len(rows) >= limit) or clock() > deadline:
                break
        result = {"count": total, "next": cursor, "complete": cursor is None,
                  "area": {k: q[k] for k in ("x1", "y1", "x2", "y2")},
                  "surface": q["surface"], "commands": commands,
                  "cost_ms": round(cost_total, 3)}
        if not count_only:
            result["entities"] = rows
        return result
