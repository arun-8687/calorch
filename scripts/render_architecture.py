"""Generate docs/architecture.html from docs/architecture.md.

The markdown source has 5 Mermaid fenced blocks; we extract them,
convert the rest of the markdown to HTML using markdown-it-py, and
assemble a self-contained HTML file that loads Mermaid from a CDN
to render the diagrams client-side.
"""
from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "architecture.md"
DST = ROOT / "docs" / "architecture.html"

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>calorch — Solution Architecture (Azure Durable Functions)</title>
<style>
  :root {{
    --bg: #0f172a;
    --bg-card: #ffffff;
    --fg: #0f172a;
    --fg-muted: #475569;
    --fg-soft: #64748b;
    --accent: #0ea5e9;
    --accent-2: #6366f1;
    --border: #e2e8f0;
    --code-bg: #f8fafc;
    --code-fg: #0f172a;
    --table-stripe: #f8fafc;
    --shadow: 0 1px 2px rgba(15,23,42,0.05), 0 8px 24px rgba(15,23,42,0.06);
    --radius: 10px;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: #f1f5f9; color: var(--fg); }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, "Helvetica Neue", sans-serif;
    line-height: 1.6;
    font-size: 16px;
  }}
  .page {{
    max-width: 1080px;
    margin: 32px auto;
    padding: 0 16px 64px;
  }}
  .doc {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 40px 56px 56px;
  }}
  .doc h1 {{
    font-size: 2rem;
    font-weight: 700;
    margin: 0 0 8px;
    color: var(--fg);
    letter-spacing: -0.02em;
  }}
  .doc > blockquote:first-of-type {{
    margin: 0 0 24px;
    padding: 12px 16px;
    background: #f0f9ff;
    border-left: 4px solid var(--accent);
    border-radius: 4px;
    color: #0c4a6e;
    font-size: 0.95rem;
  }}
  .doc h2 {{
    font-size: 1.5rem;
    font-weight: 700;
    margin: 40px 0 12px;
    padding-bottom: 8px;
    border-bottom: 2px solid var(--border);
    color: var(--fg);
  }}
  .doc h3 {{
    font-size: 1.15rem;
    font-weight: 600;
    margin: 24px 0 8px;
    color: var(--fg);
  }}
  .doc p {{ margin: 8px 0 12px; color: var(--fg-muted); }}
  .doc ul, .doc ol {{ padding-left: 1.5em; color: var(--fg-muted); }}
  .doc li {{ margin: 4px 0; }}
  .doc strong {{ color: var(--fg); }}
  .doc a {{ color: var(--accent-2); text-decoration: none; border-bottom: 1px dotted var(--accent-2); }}
  .doc a:hover {{ border-bottom-style: solid; }}
  .doc hr {{
    border: 0;
    height: 1px;
    background: var(--border);
    margin: 32px 0;
  }}
  .doc code {{
    background: var(--code-bg);
    color: var(--code-fg);
    padding: 2px 6px;
    border-radius: 4px;
    font-family: "SFMono-Regular", "Consolas", "Liberation Mono", Menlo, monospace;
    font-size: 0.9em;
    border: 1px solid var(--border);
  }}
  .doc pre {{
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    overflow-x: auto;
    font-size: 0.85em;
    line-height: 1.5;
  }}
  .doc pre code {{
    background: transparent;
    border: 0;
    padding: 0;
  }}
  .doc table {{
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 0.92em;
  }}
  .doc th, .doc td {{
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  .doc th {{
    background: #f8fafc;
    font-weight: 600;
    color: var(--fg);
    border-bottom: 2px solid var(--border);
  }}
  .doc tbody tr:nth-child(even) {{ background: var(--table-stripe); }}
  .doc .mermaid {{
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 24px 16px;
    margin: 24px 0;
    overflow-x: auto;
    text-align: center;
  }}
  .doc .footer {{
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--fg-soft);
    font-size: 0.9em;
  }}
  .toc {{
    position: sticky;
    top: 16px;
    float: right;
    width: 220px;
    margin: 0 0 16px 16px;
    padding: 12px 16px;
    background: #f8fafc;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 0.85em;
    max-height: 80vh;
    overflow-y: auto;
  }}
  .toc h4 {{
    margin: 0 0 8px;
    font-size: 0.75em;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-soft);
  }}
  .toc ul {{ list-style: none; padding: 0; margin: 0; }}
  .toc li {{ padding: 2px 0; }}
  .toc a {{ color: var(--fg-muted); border: 0; }}
  .toc a:hover {{ color: var(--accent); }}
  @media (max-width: 900px) {{
    .toc {{ position: static; float: none; width: auto; max-height: none; }}
    .doc {{ padding: 24px; }}
  }}
  @media print {{
    body {{ background: white; }}
    .page {{ margin: 0; padding: 0; max-width: none; }}
    .doc {{ box-shadow: none; border: 0; padding: 0; }}
    .toc {{ display: none; }}
    .doc h2 {{ page-break-before: always; }}
    .doc h2:first-of-type {{ page-break-before: avoid; }}
    .doc .mermaid {{ page-break-inside: avoid; }}
  }}
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.75em;
    font-weight: 600;
    background: #e0f2fe;
    color: #0c4a6e;
    margin-right: 6px;
  }}
</style>
</head>
<body>
<div class="page">
<div class="doc">
{body}
</div>
</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
  // Mermaid source blocks, embedded as a JSON array to avoid markdown-it
  // mangling brackets/parens inside the diagram definitions.
  const MERMAID_DIAGRAMS = {diagrams_json};
  document.addEventListener("DOMContentLoaded", function() {{
    // Hydrate placeholders
    document.querySelectorAll(".mermaid-placeholder").forEach(function(el) {{
      const idx = parseInt(el.getAttribute("data-idx"), 10);
      const div = document.createElement("div");
      div.className = "mermaid";
      div.textContent = MERMAID_DIAGRAMS[idx];
      el.replaceWith(div);
    }});
    mermaid.initialize({{
      startOnLoad: true,
      theme: "default",
      securityLevel: "loose",
      flowchart: {{ useMaxWidth: true, htmlLabels: true, curve: "basis" }},
      sequence: {{ useMaxWidth: true, showSequenceNumbers: false }},
      themeVariables: {{
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        fontSize: "13px"
      }}
    }});
  }});
</script>
</body>
</html>
"""


def extract_mermaid(md: str) -> tuple[str, list[str]]:
    """Replace each ```mermaid block with a Mermaid-immune placeholder.

    markdown-it would otherwise interpret Mermaid's brackets/parens as
    inline code/links. We stash the Mermaid source in a global JS
    variable and emit a placeholder div that the script copies into.
    """
    diagrams: list[str] = []

    def _repl(m: re.Match) -> str:
        idx = len(diagrams)
        diagrams.append(m.group(1))
        # Use a span placeholder; the Mermaid initializer script below
        # walks the DOM and replaces each placeholder with its diagram.
        return f'<div class="mermaid-placeholder" data-idx="{idx}"></div>'

    out = re.sub(r"```mermaid\n(.*?)\n```", _repl, md, flags=re.DOTALL)
    return out, diagrams


def slugify(text: str) -> str:
    """Make a URL-safe slug from heading text."""
    s = text.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def add_heading_ids(html: str) -> str:
    """Add id="..." attributes to <h2> and <h3> tags, and collect them."""
    seen: dict[str, int] = {}

    def _repl(m: re.Match) -> str:
        level, inner = m.group(1), m.group(2)
        # Strip tags from inner text to compute slug
        text = re.sub(r"<[^>]+>", "", inner)
        slug = slugify(text) or "section"
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        if n:
            slug = f"{slug}-{n}"
        return f"<h{level} id=\"{slug}\">{inner}</h{level}>"

    out = re.sub(r"<h([23])>(.*?)</h\1>", _repl, html, flags=re.DOTALL)
    return out


def build_toc(html: str) -> str:
    """Build a sticky TOC of <h2> headings."""
    headings = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', html, flags=re.DOTALL)
    if not headings:
        return ""
    items = "\n".join(
        f'<li><a href="#{slug}">{re.sub(r"<[^>]+>", "", text)}</a></li>'
        for slug, text in headings
    )
    return f'<aside class="toc">\n<h4>Contents</h4>\n<ul>\n{items}\n</ul>\n</aside>'


def main() -> int:
    import json

    md = SRC.read_text(encoding="utf-8")
    md_with_placeholders, diagrams = extract_mermaid(md)

    md_engine = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": True})
    md_engine.enable("table")
    md_engine.enable("strikethrough")
    body_html = md_engine.render(md_with_placeholders)

    body_html = add_heading_ids(body_html)
    toc_html = build_toc(body_html)

    # JSON-encode the diagrams to a string safe for inlining in <script>.
    diagrams_json = json.dumps(diagrams)

    html = HTML_TEMPLATE.format(body=toc_html + body_html, diagrams_json=diagrams_json)

    DST.write_text(html, encoding="utf-8")
    print(f"[ok] wrote {DST}  ({len(html):,} bytes, {len(diagrams)} mermaid blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
