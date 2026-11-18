"""A stand-in for `codex app-server`, speaking line-delimited JSON-RPC on stdio.

It implements just what banxing/context/appserver.py uses, following the published
protocol description. It is synthetic: tests that pass against it show the client
is self-consistent, not that it matches a real Codex build.

Usage: fake_appserver.py <data.json> [mode]
  mode: full (default) | old (no thread/turns/list) | strict (rejects sortKey /
        sortDirection) | silent (never answers after initialize) | asks (sends a
        server request before answering) | summary (turns come without their items;
        thread/items/list has them) | plainview (does not know itemsView)

Paging: `limit` and `cursor` are honoured and `nextCursor` is null on the last page.
"""
import json
import sys


def main():
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
    mode = sys.argv[2] if len(sys.argv) > 2 else "full"
    initialized = False
    log = open(sys.argv[1] + ".log", "a", encoding="utf-8")

    def send(message):
        # Bytes, so the console code page of the machine running the tests does not matter.
        sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()

    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        log.write(json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n")
        log.flush()
        if request_id is None:
            if method == "initialized":
                initialized = True
            continue
        if method == "initialize":
            send({"id": request_id, "result": {"userAgent": "fake-codex/0.0"}})
            continue
        if not initialized:
            send({"id": request_id, "error": {"code": -32002, "message": "Not initialized"}})
            continue
        if mode == "silent":
            continue
        if mode == "asks":
            send({"id": 9000 + request_id, "method": "item/commandExecution/requestApproval", "params": {}})
        if method == "thread/list":
            if mode == "strict" and "sortKey" in params:
                send({"id": request_id, "error": {"code": -32602, "message": "unknown field sortKey"}})
                continue
            threads = sorted(data["threads"], key=lambda t: -t.get("updatedAt", 0))[: params.get("limit", 50)]
            send({"id": request_id, "result": {"data": threads, "nextCursor": None}})
        elif method == "thread/turns/list":
            if mode in ("old", "strict"):
                code = -32601 if mode == "old" else -32602
                send({"id": request_id, "error": {"code": code, "message": "unsupported"}})
                continue
            turns = data["turns"].get(params.get("threadId"))
            if turns is None:
                send({"id": request_id, "error": {"code": -32600, "message": "thread not found"}})
                continue
            if mode == "plainview" and "itemsView" in params:
                send({"id": request_id, "error": {"code": -32602, "message": "unknown field itemsView"}})
                continue
            ordered = list(reversed(turns)) if params.get("sortDirection") == "desc" else list(turns)
            if mode == "summary":
                ordered = [dict(turn, items=[]) for turn in ordered]
            start, size = int(params.get("cursor") or 0), params.get("limit", 50)
            following = str(start + size) if start + size < len(ordered) else None
            send({"id": request_id, "result": {"data": ordered[start:start + size], "nextCursor": following}})
        elif method == "thread/items/list":
            if mode != "summary":
                send({"id": request_id, "error": {"code": -32601, "message": "item paging is not supported"}})
                continue
            entries = [{"turnId": turn.get("id"), "item": item}
                       for turn in data["turns"].get(params.get("threadId"), []) for item in turn.get("items", [])]
            start, size = int(params.get("cursor") or 0), params.get("limit", 50)
            following = str(start + size) if start + size < len(entries) else None
            send({"id": request_id, "result": {"data": entries[start:start + size], "nextCursor": following}})
        elif method == "thread/read":
            turns = data["turns"].get(params.get("threadId"), [])
            thread = {"id": params.get("threadId"), "turns": turns if params.get("includeTurns") else []}
            send({"id": request_id, "result": {"thread": thread}})
        else:
            send({"id": request_id, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
