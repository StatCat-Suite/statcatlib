"""Unit tests for the dependency-free XHTML → markdown converter used to
down-convert reference-metadata sections for the search corpus."""

from __future__ import annotations

from statcatlib.store.xhtml import xhtml_to_markdown


def test_plain_text_passthrough() -> None:
    assert xhtml_to_markdown("  Just plain text.  ") == "Just plain text."


def test_headings_and_paragraphs() -> None:
    md = xhtml_to_markdown("<h2>Data quality</h2><p>Reached 95.0 percent.</p><h3>Linkage</h3><p>Detail.</p>")
    assert "## Data quality" in md
    assert "### Linkage" in md
    assert "Reached 95.0 percent." in md
    # blocks are blank-line separated (chunk boundaries)
    assert "\n\n" in md


def test_lists_become_bullets() -> None:
    md = xhtml_to_markdown("<ul><li>First</li><li>Second<ul><li>Nested</li></ul></li></ul>")
    assert "- First" in md
    assert "- Second" in md
    assert "  - Nested" in md  # nested list indented


def test_ordered_list_numbers() -> None:
    md = xhtml_to_markdown("<ol><li>One</li><li>Two</li></ol>")
    assert "1. One" in md
    assert "2. Two" in md


def test_table_becomes_markdown() -> None:
    xhtml = (
        "<table><caption>Table 1 Rates</caption>"
        '<tr><th scope="col">Geo</th><th>Rate</th></tr>'
        "<tr><td>Canada</td><td>3.5</td></tr>"
        "<tr><td>Quebec</td><td>4.1</td></tr></table>"
    )
    md = xhtml_to_markdown(xhtml)
    assert "**Table 1 Rates**" in md
    assert "| Geo | Rate |" in md
    assert "| --- | --- |" in md
    assert "| Canada | 3.5 |" in md
    # the table is one atomic block (single newlines between rows)
    assert "| Geo | Rate |\n| --- | --- |\n| Canada | 3.5 |" in md


def test_thead_tbody_rows_collected() -> None:
    xhtml = "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>"
    md = xhtml_to_markdown(xhtml)
    assert "| A |" in md
    assert "| 1 |" in md


def test_entities_decoded_and_tags_stripped() -> None:
    md = xhtml_to_markdown("<p>Tom &amp; Jerry <sup><a>Note 1</a></sup> end</p>")
    assert "Tom & Jerry" in md
    assert "Note 1" in md
    assert "<" not in md  # no markup survives


def test_definition_list() -> None:
    md = xhtml_to_markdown("<dl><dt>Term</dt><dd>Definition.</dd></dl>")
    assert "**Term**" in md
    assert "Definition." in md


def test_no_html_tags_in_output() -> None:
    xhtml = "<h3>H</h3><p>para <em>emph</em> and <strong>bold</strong></p><table><tr><td>x</td></tr></table>"
    md = xhtml_to_markdown(xhtml)
    assert "<" not in md
    assert ">" not in md
