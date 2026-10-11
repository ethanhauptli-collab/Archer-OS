"""The paper edit as a clean, printable web page (edit_report.html).

A small converter for exactly the Markdown `report.build_report` writes:
headings, nested bullets with <sub> continuation lines, to-do checkboxes,
bold/italic/code, block quotes, tables and the fenced Chapters block, which
gets a "Copy chapters" button for the YouTube description. Everything the
model or a file name contributed is HTML-escaped. No external files, so the
page works from Finder (file://) and prints well.
"""

from __future__ import annotations

import html
import re

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])_(?!_)(.+?)(?<!_)_(?![\w])")
_SUB = re.compile(r"&lt;sub&gt;(.*?)&lt;/sub&gt;")


def inline(text: str) -> str:
    """Escape, then bold/italic/code. Code spans are protected from the other two."""
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE.sub(keep, text)
    text = html.escape(text, quote=False)
    text = _SUB.sub(r"<sub>\1</sub>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{html.escape(codes[int(m.group(1))], quote=False)}</code>", text)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head, body = cells[0], [r for r in cells[1:] if not all(set(c) <= set("-: ") for c in r)]
    out = ["<table>", "<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead>", "<tbody>"]
    out += ["<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body]
    out += ["</tbody>", "</table>"]
    return "\n".join(out)


def _list(lines: list[str]) -> str:
    """'- item', '  - nested item', '  continuation' (the <sub> source line) and '- [ ] to-do'."""
    out: list[str] = []
    depth = -1
    for line in lines:
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if not body.startswith("- "):  # a continuation of the item above
            out[-1] = out[-1].removesuffix("</li>") + "<br>" + inline(body) + "</li>"
            continue
        level = 1 if indent >= 2 else 0
        while depth < level:
            if out and depth >= 0:
                out[-1] = out[-1].removesuffix("</li>")  # nest inside the open item
            out.append("<ul>")
            depth += 1
        while depth > level:
            out.append("</ul></li>")
            depth -= 1
        item = body[2:]
        if item.startswith("[ ] "):
            out.append(f'<li class="todo"><span class="box" aria-hidden="true"></span>{inline(item[4:])}</li>')
        else:
            out.append(f"<li>{inline(item.rstrip())}</li>")
    while depth > 0:
        out.append("</ul></li>")
        depth -= 1
    out.append("</ul>")
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    heading = ""
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            text = html.escape("\n".join(lines[i + 1 : j]), quote=False)
            if heading == "Chapters":
                out.append(
                    '<div class="chapters"><pre id="chapter-list">' + text + "</pre>"
                    '<button type="button" id="copy-chapters" onclick="copyChapters(this)">Copy chapters</button></div>'
                )
            else:
                out.append(f"<pre>{text}</pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,3}) (.*)$", line)
        if m:
            level, text = len(m.group(1)), m.group(2)
            heading = re.sub(r"\s*`[^`]*`\s*$", "", text).strip()
            out.append(f'<h{level} id="{_slug(heading)}">{inline(text)}</h{level}>')
            i += 1
            continue
        if line.startswith(">"):
            block = []
            while i < len(lines) and lines[i].startswith(">"):
                block.append(lines[i][1:].strip())
                i += 1
            paras = "\n".join(block).split("\n\n")
            out.append("<blockquote>" + "".join(f"<p>{'<br>'.join(inline(t) for t in p.splitlines())}</p>" for p in paras if p.strip()) + "</blockquote>")
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip() and (lines[i].startswith("- ") or lines[i].startswith("  ")):
                items.append(lines[i])
                i += 1
            out.append(_list(items))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,3} |```|>|\||- )", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>".join(inline(t) for t in para) + "</p>")
    return "\n".join(out)


_STYLE = """
:root { --ink: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7; --tint: #f5f5f7; --accent: #0a66d8; --paper: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #f5f5f7; --muted: #a1a1a6; --line: #3a3a3c; --tint: #1c1c1e; --accent: #4c9bff; --paper: #000000; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 28px; line-height: 1.2; margin: 0 0 8px; }
h2 { font-size: 19px; margin: 36px 0 10px; padding-top: 12px; border-top: 1px solid var(--line); }
h3 { font-size: 16px; margin: 24px 0 6px; }
h3 code { font-weight: normal; }
p { margin: 8px 0; }
ul { margin: 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
li li { color: var(--muted); }
sub { vertical-align: baseline; font-size: 12px; color: var(--muted); }
code { font: 12.5px ui-monospace, "SF Mono", Menlo, monospace; background: var(--tint); padding: 1px 4px; border-radius: 4px; }
blockquote { margin: 12px 0; padding: 10px 14px; border-left: 3px solid var(--accent); background: var(--tint); border-radius: 0 6px 6px 0; }
blockquote p { margin: 4px 0; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line); vertical-align: top; overflow-wrap: anywhere; }
th { color: var(--muted); font-weight: 600; }
.chapters { display: flex; gap: 12px; align-items: flex-start; flex-wrap: wrap; }
pre { margin: 6px 0; padding: 10px 14px; background: var(--tint); border-radius: 8px; font: 13px/1.5 ui-monospace, "SF Mono", Menlo, monospace; overflow-x: auto; }
.chapters pre { flex: 1 1 260px; }
button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 6px; border: 1px solid var(--line); background: var(--paper); color: var(--ink); cursor: pointer; }
button:hover { border-color: var(--accent); }
li.todo { list-style: none; margin-left: -20px; }
li.todo .box { display: inline-block; width: 12px; height: 12px; border: 1.5px solid var(--muted); border-radius: 3px; margin-right: 8px; vertical-align: -1px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 18px; }
@media (max-width: 600px) { main { padding: 20px 16px 48px; } table { font-size: 12px; } }
@media print {
  :root { --ink: #000; --muted: #444; --line: #bbb; --tint: #f2f2f2; --paper: #fff; }
  body { font-size: 11pt; }
  main { max-width: none; padding: 0; }
  button { display: none; }
  h2 { break-after: avoid; }
  h3, li, tr, blockquote, pre { break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
}
"""

_SCRIPT = """
function copyChapters(button) {
  var pre = document.getElementById('chapter-list');
  var range = document.createRange();
  range.selectNodeContents(pre);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  var done = function () { button.textContent = 'Copied'; setTimeout(function () { button.textContent = 'Copy chapters'; }, 1600); };
  var manual = function () {
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    if (ok) { done(); } else { button.textContent = 'Selected: press ⌘C'; }
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(pre.innerText).then(done, manual);
  } else {
    manual();
  }
}
"""


def report_page(md: str, *, title: str, subtitle: str = "") -> str:
    """A complete, self-contained page for the paper edit."""
    body = markdown_to_html(md)
    meta = f'<div class="meta">{html.escape(subtitle)}</div>' if subtitle else ""
    if meta and "</h1>" in body:
        body = body.replace("</h1>", "</h1>\n" + meta, 1)
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n"
        f"{body}\n</main>\n<script>{_SCRIPT}</script>\n</body>\n</html>\n"
    )
