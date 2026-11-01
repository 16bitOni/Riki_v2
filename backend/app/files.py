"""Files you give Riki: PDF, Word, Excel, CSV and text, turned into readable lines it can page through.

An upload is stored as an artifact of kind "upload" holding the extracted text (the original bytes are not kept).
``read_file`` reads it the way ``fetch_url`` reads a page: a part at a time, or only the lines that match.
shortcut: spreadsheets are read as tab-separated rows (first 500 per sheet), without formulas; for real numbers
(sums, charts) run the file through run_python, which needs RIKI_SANDBOX=true.
"""

import io
import json

from langchain_core.tools import tool

from .agent.policy import SAFE
from .agent.tools import ToolEntry, page_view, wrap_lines
from .db import Artifact, utcnow

TEXT_TYPES = {"txt", "md", "csv", "tsv", "json", "log", "yaml", "yml", "xml", "html", "py", "js", "ts", "sql", "ini", "toml"}
MAX_CHARS = 2_000_000
MAX_ROWS = 500


class Unreadable(ValueError):
    """The file cannot be turned into text; the message is for the user."""


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def extract_text(name: str, data: bytes) -> str:
    ext = extension(name)
    if ext in TEXT_TYPES or not ext:
        return data.decode("utf-8", errors="replace")[:MAX_CHARS]
    try:
        if ext == "pdf":
            from pypdf import PdfReader

            pages = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
            text = "\n\n".join(f"--- page {i + 1} ---\n{t}" for i, t in enumerate(pages) if t.strip())
            if not text.strip():
                raise Unreadable("That PDF has no text in it (it looks scanned). Riki cannot read scanned pages yet.")
            return text[:MAX_CHARS]
        if ext == "docx":
            from docx import Document

            doc = Document(io.BytesIO(data))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                parts += ["\t".join(c.text.strip() for c in row.cells) for row in table.rows]
            return "\n".join(parts)[:MAX_CHARS]
        if ext in ("xlsx", "xlsm"):
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            out = []
            for ws in wb.worksheets:
                out.append(f"--- sheet {ws.title} ---")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= MAX_ROWS:
                        out.append(f"[... more rows not shown; {MAX_ROWS} per sheet]")
                        break
                    out.append("\t".join("" if v is None else str(v) for v in row))
            return "\n".join(out)[:MAX_CHARS]
    except Unreadable:
        raise
    except ImportError as exc:
        raise Unreadable(f"Reading .{ext} files needs a package that is not installed ({exc.name}). Run uv sync.")
    except Exception as exc:
        raise Unreadable(f"Could not read that .{ext} file: {type(exc).__name__}.")
    raise Unreadable(f"Riki cannot read .{ext} files. Use PDF, Word, Excel, CSV or text.")


async def save_upload(rt, name: str, data: bytes) -> dict:
    name = (name or "file").strip()[:200] or "file"
    text = extract_text(name, data)
    if not text.strip():
        raise Unreadable("That file is empty.")
    async with rt.db.session() as db:
        a = Artifact(kind="upload", format=extension(name)[:8], title=name, content=text, updated_at=utcnow())
        db.add(a)
        await db.commit()
    return {"id": a.id, "name": name, "chars": len(text), "lines": len(wrap_lines(text))}


def file_tools(rt) -> list[ToolEntry]:
    s = rt.settings

    @tool
    async def read_file(id: str, start_line: int = 1, find: str = "") -> str:
        """Read a file the user attached (its id is in their message) as numbered lines, one part at a time. To read on,
        call again with the start_line it gives; to jump to something, pass find="words" for only the lines with them all."""
        async with rt.db.session() as db:
            a = await db.get(Artifact, id.strip()) if len(id.strip()) == 32 else None
        if a is None or a.kind != "upload":
            return f"No attached file with id {id}."
        lines = wrap_lines(a.content)
        budget = max(s.tool_output_chars - 300, 300)
        return page_view(f"[{a.title} · {len(lines)} lines", lines, start_line, find, budget, what="file")

    return [ToolEntry(read_file, SAFE)]
