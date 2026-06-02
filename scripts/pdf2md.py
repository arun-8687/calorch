"""Convert PDF to markdown using pdfplumber for text + tables."""
import sys
from pathlib import Path

import pdfplumber


def pdf_to_md(pdf_path: Path, md_path: Path) -> None:
    with pdfplumber.open(pdf_path) as pdf:
        lines: list[str] = []
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            # Clean up common pdfplumber artifacts
            text = text.replace("\x0c", "")  # form feeds
            if text.strip():
                lines.append(f"## Page {i}\n")
                lines.append(text)
                lines.append("")

            # Extract tables on this page
            tables = page.extract_tables()
            for j, table in enumerate(tables, start=1):
                if not table or len(table) < 2:
                    continue
                lines.append(f"### Table {j} (page {i})\n")
                # Header row
                header = table[0]
                lines.append("| " + " | ".join(str(c or "") for c in header) + " |")
                lines.append("| " + " | ".join("---" for _ in header) + " |")
                # Data rows
                for row in table[1:]:
                    lines.append("| " + " | ".join(str(c or "") for c in row) + " |")
                lines.append("")

        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Converted {pdf_path} -> {md_path} ({len(pdf.pages)} pages, {len(lines)} lines)")


if __name__ == "__main__":
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\workspace\calorch\DOC-20260328-WA0000 (1).pdf")
    md_path = pdf_path.with_suffix(".md")
    pdf_to_md(pdf_path, md_path)