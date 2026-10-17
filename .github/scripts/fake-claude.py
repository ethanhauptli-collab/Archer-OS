#!/usr/bin/env python3
"""A stand-in for Claude Code (`claude`) for the Mac end-to-end run: it answers Roughcut's real requests from the request itself,
so the AI-planned build, the motion graphics and a revision can be driven through the packaged app on a runner with no account.

Roughcut calls `claude -p --output-format json --json-schema SCHEMA ...` with the prompt on stdin, and `claude auth status --json`.
This program tells the requests apart by the schema, reads the clip and segment IDs out of the prompt, and returns:
  * the visual log: a line for each clip asked about;
  * the edit plan: every spoken line in order, the B-roll and stills laid over them, and (when asked) a lower third and a stat;
  * the graphics designs: a small plate in the renderer's contract (CSS animation plus a seek hook) for each id asked for;
  * a revision (the plan asked for again): the same plan without its last spoken line.
FAKE_CLAUDE_STATE names a JSON file the run can change between steps; {"limit": "design"} (or "plan", "vision") makes that kind of
request answer as Claude Code does when a plan is used up, and {"slow": "design"} makes it wait a minute ("slow_seconds") before it
answers. Every call is logged to FAKE_CLAUDE_STATE + ".log".
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("FAKE_CLAUDE_STATE") or Path.home() / ".fake-claude-state.json")


def state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def log(entry: dict) -> None:
    with open(str(STATE) + ".log", "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def reply(out: dict | None = None, *, error: str | None = None) -> None:
    if error:
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": error, "usage": {}}))
        sys.exit(1)
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": out,
        "usage": {"input_tokens": 1200, "output_tokens": 300}, "permission_denials": [],
    }))
    sys.exit(0)


PLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:transparent;overflow:hidden}
.plate{position:absolute;left:6%;bottom:14%;padding:14px 26px;background:#F26B21;color:#fff;border-radius:6px;
 font:700 54px -apple-system,'Helvetica Neue',Arial,sans-serif;
 animation:in .45s ease-out both,out .25s ease-in calc(var(--d,3s) - .4s) both}
.sub{display:block;font:500 28px -apple-system,'Helvetica Neue',Arial,sans-serif;opacity:.9;margin-top:4px}
@keyframes in{from{transform:translateX(-120%)}to{transform:none}}
@keyframes out{to{opacity:0}}
</style></head><body><div class="plate">MAIN<span class="sub">SUB</span></div>
<script>window.seek=function(t){}</script>
</body></html>"""


def main() -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("2.1.301 (Claude Code)")
        return
    if args[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        return
    prompt = sys.stdin.read()
    if "--json-schema" not in args:
        reply(error="the stand-in only answers requests with a schema")
    schema = json.loads(args[args.index("--json-schema") + 1])
    props = schema.get("properties", {})
    kind = "plan" if "sections" in props else "design" if "style" in props else "vision"
    log({"kind": kind, "argv": args[:6], "bytes": len(prompt), "limit": state().get("limit")})
    if state().get("limit") == kind:
        reply(error="You've hit your limit · resets 5pm")
    if state().get("slow") == kind:  # a request that takes a minute: Cancel can be pressed while it runs
        time.sleep(float(state().get("slow_seconds", 60)))

    if kind == "vision":
        asked = re.findall(r'^Clip (\w+) "([^"]*)"', prompt, re.M)
        moments = "moments" in json.dumps(props)
        if moments:
            reply({"clips": [{"id": clip, "moments": []} for clip, _ in asked]})
        reply({"clips": [
            {"id": clip, "description": f"{name}: a test pattern", "tags": ["test", "pattern"], "shot": "wide", "quality": "good"}
            for clip, name in asked
        ]})

    if kind == "design":
        block = prompt.split("<graphics>")[-1]
        found = re.findall(r"\b(G\d\d)\b[^\n]*", block)
        texts = {}
        for match in re.finditer(r"\b(G\d\d)\b[^\n]*\n((?:[ \t]+[^\n]*\n)*)", block):
            body = match.group(0)
            quoted = re.findall(r'"([^"]+)"', body)
            texts[match.group(1)] = (quoted[0] if quoted else match.group(1), quoted[1] if len(quoted) > 1 else "")
        ids = list(dict.fromkeys(found))
        reply({"style": "Deep navy plates are not used here: a solid orange plate, white type, one slide-in.", "graphics": [
            {"id": i, "html": PLATE.replace("MAIN", texts.get(i, (i, ""))[0]).replace("SUB", texts.get(i, (i, ""))[1])} for i in ids
        ]})

    # The edit plan. Lines look like: V01.S001 [0:00.0-0:01.1 | 1.1s → 1.1s] Welcome to Orange County.
    lines = re.findall(r"^([A-Z]\d+\.S\d+) \[[^\]]*\] (.*)$", prompt, re.M)
    others = re.findall(r'^## ([VAI]\d+) "[^"]*", (?:video|image|audio)[^\n]*role (B-Roll|Stills|Music)', prompt, re.M)
    music = [clip for clip, role in others if role == "Music"]
    cutaways = [clip for clip, role in others if role in ("B-Roll", "Stills")]
    revising = "<editor_notes>" in prompt or "<revision" in prompt or "REVISION" in prompt
    # "Use a bolder lower third" in a revision's notes: the lower third's words change, so it needs a design of its own.
    retitled = revising and "bolder lower third" in prompt.lower()
    if revising and len(lines) > 1:
        lines = lines[:-1]
    ids = [i for i, _ in lines]
    broll = []
    for n, clip in enumerate(cutaways[:2]):
        if len(ids) > n + 1:
            broll.append({"clip": clip, "over": [ids[n + 1]], "words": "", "source_in": -1, "reason": "something to look at while it is said"})
    plan = {
        "title": "Walk and talk",
        "logline": "A walk through the festival grounds.",
        "sections": [{"name": "Walk and talk", "purpose": "the whole shoot, in order", "items": ids, "broll": broll}],
        "alternates": [],
        "flags": [],
        "music": [{"clip": music[0], "first_section": 0, "last_section": 0, "source_in": 0}] if music else [],
        "cut_notes": "Dropped the last line." if revising else "Kept every line; the pause was cut.",
        "spellings": [],
        "pacing": "tight" if "punchy" in prompt.lower() else "unspecified",
        "punch_ins": "unspecified",
        "graphics_asked": "graphics" in props,
        "redesign_graphics": "unspecified",
        "sfx": "unspecified",
    }
    if "graphics" in props:
        graphics = []
        if lines:
            first = lines[0]
            words = " ".join(re.sub(r"[^\w']", "", w) for w in first[1].split()[:3])
            graphics.append({"kind": "lower_third", "segment": first[0], "words": words, "text": "Orange County Food Festival" if retitled else "Orange County", "subtext": "Food festival",
                             "seconds": 3, "direction": ""})
        if len(lines) > 1:
            second = lines[-1]
            words = " ".join(re.sub(r"[^\w']", "", w) for w in second[1].split()[-2:])
            graphics.append({"kind": "stat", "segment": second[0], "words": words, "text": "$20", "subtext": "tickets start here",
                             "seconds": 3, "direction": ""})
        plan["graphics"] = graphics
    reply(plan)


main()
