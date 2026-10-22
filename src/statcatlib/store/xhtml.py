"""Dependency-free XHTML → markdown for reference-metadata indexing.

SDMX reference metadata stores document sections as XHTML fragments
(``text_type=XHTML``) — the standards-native, lossless form served by a
REST/query view. For the *search* corpus that form is wrong: HTML markup
is low-semantic-value noise that dilutes an embedding and wastes the
encoder's token window. This module down-converts the XHTML to compact
markdown at index time, so the retrieval representation is token-efficient
and LLM-friendly while the canonical store stays XHTML.

Deliberately tiny and dependency-free (stdlib `html.parser`, no
BeautifulSoup): it targets the controlled tag subset producers emit
(headings, paragraphs, lists, tables with captions, definition lists,
blockquotes, inline emphasis/links). Unknown tags degrade to their text.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING, NotRequired, TypedDict, override

if TYPE_CHECKING:
    from collections.abc import Callable


class _Node(TypedDict):
    """One node of the parsed fragment.

    A `TypedDict` rather than `_Node`: the shape is known and small,
    and `Any` here would erase the tree's type through every renderer below.
    ``children`` is always present — a ``#text`` node carries an empty list —
    so the ten direct ``node["children"]`` accesses stay checked. ``text`` is
    only on ``#text`` nodes, hence `NotRequired` and the ``.get`` at its two
    call sites.
    """

    tag: str
    children: list[_Node]
    text: NotRequired[str]


_VOID = {"br"}
_HEADING = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
_ROW_GROUPS = {"thead", "tbody", "tfoot"}


class _TreeBuilder(HTMLParser):
    """Build a minimal ``{tag, attrs, children}`` tree from an XHTML fragment.

    Lenient by design (``html.parser`` tolerates the unclosed/void tags and
    entities real-world fragments carry). ``convert_charrefs`` decodes
    entities into text so the render stage sees plain characters.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: _Node = {"tag": "root", "children": []}
        self._stack: list[_Node] = [self.root]

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node: _Node = {"tag": tag, "children": []}
        self._stack[-1]["children"].append(node)
        if tag not in _VOID:
            self._stack.append(node)

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1]["children"].append({"tag": tag, "children": []})

    @override
    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                return

    @override
    def handle_data(self, data: str) -> None:
        self._stack[-1]["children"].append({"tag": "#text", "children": [], "text": data})


def _text(node: _Node) -> str:
    """Collapsed inline text of a node (``<br>`` → space)."""
    out: list[str] = []
    for c in node.get("children", ()):
        if c["tag"] == "#text":
            out.append(c.get("text", ""))
        elif c["tag"] == "br":
            out.append(" ")
        else:
            out.append(_text(c))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _render_list(node: _Node, depth: int = 0) -> list[str]:
    ordered = node["tag"] == "ol"
    indent = "  " * depth
    lines: list[str] = []
    n = 0
    for li in node["children"]:
        if li["tag"] != "li":
            continue
        n += 1
        direct: list[str] = []
        sublists: list[_Node] = []
        for c in li["children"]:
            if c["tag"] in ("ul", "ol"):
                sublists.append(c)
            elif c["tag"] == "#text":
                direct.append(c.get("text", ""))
            elif c["tag"] == "br":
                direct.append(" ")
            else:
                direct.append(_text(c))
        text = re.sub(r"\s+", " ", "".join(direct)).strip()
        marker = f"{n}." if ordered else "-"
        if text:
            lines.append(f"{indent}{marker} {text}")
        for sub in sublists:
            lines.extend(_render_list(sub, depth + 1))
    return lines


def _collect_rows(node: _Node, rows: list[str]) -> None:
    for c in node["children"]:
        if c["tag"] == "tr":
            cells = [_text(cell) for cell in c["children"] if cell["tag"] in ("th", "td")]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        elif c["tag"] in _ROW_GROUPS:
            _collect_rows(c, rows)


def _render_table(node: _Node) -> str:
    parts: list[str] = []
    for c in node["children"]:
        if c["tag"] == "caption":
            cap = _text(c)
            if cap:
                parts.append(f"**{cap}**")
    rows: list[str] = []
    _collect_rows(node, rows)
    if rows:
        ncol = max(rows[0].count("|") - 1, 1)
        rows.insert(1, "| " + " | ".join(["---"] * ncol) + " |")
        parts.append("\n".join(rows))
    return "\n\n".join(parts)


def _render_definition_list(node: _Node) -> str:
    parts: list[str] = []
    for c in node["children"]:
        text = _text(c)
        if not text:
            continue
        parts.append(f"**{text}**" if c["tag"] == "dt" else text)
    return "\n\n".join(parts)


def _render_blockquote(node: _Node) -> str:
    text = _text(node)
    return f"> {text}" if text else ""


# tag → block renderer. Headings and bare text are handled inline in
# ``_render``; everything else that isn't a plain paragraph dispatches here.
_BLOCK_RENDERERS: dict[str, Callable[[_Node], str]] = {
    "ul": lambda n: "\n".join(_render_list(n)),
    "ol": lambda n: "\n".join(_render_list(n)),
    "table": _render_table,
    "dl": _render_definition_list,
    "blockquote": _render_blockquote,
}


def _render_block(node: _Node) -> str:
    tag = node["tag"]
    if tag in _HEADING:
        text = _text(node)
        return f"{_HEADING[tag]} {text}" if text else ""
    if tag in _BLOCK_RENDERERS:
        return _BLOCK_RENDERERS[tag](node)
    if tag == "#text":
        return re.sub(r"\s+", " ", node.get("text", "")).strip()
    return _text(node)  # p and any other wrapper — inline text as a paragraph


def _render(nodes: list[_Node]) -> str:
    blocks = (_render_block(node) for node in nodes)
    return "\n\n".join(b for b in blocks if b)


def xhtml_to_markdown(html: str) -> str:
    """Convert an XHTML fragment to compact markdown.

    A string with no ``<`` is returned trimmed (already plain text), so the
    call is a safe no-op on non-XHTML values.
    """
    if "<" not in html:
        return html.strip()
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return _render(builder.root["children"])
