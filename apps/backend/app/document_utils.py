"""Phase 10 (knowledge base): turns Word/Excel files into plain text before
they go to Anthropic's Files API, which reads PDF, plain text, and images
natively but not .docx/.xlsx directly. Used by knowledge_routes.py at
upload time - never on the read/ask path."""
import io


def extract_docx_text(content: bytes) -> str:
    """Pulls paragraph text and table cell text out of a .docx file, in
    document order (tables are appended as pipe-separated rows so the model
    can still make sense of tabular data)."""
    import docx

    doc = docx.Document(io.BytesIO(content))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()


def extract_xlsx_text(content: bytes) -> str:
    """Pulls every sheet's cell values out of a .xlsx file as pipe-separated
    rows, prefixed with a "--- Sheet: <name> ---" marker per sheet so the
    model can tell which sheet a row came from. data_only=True reads the
    last-calculated value of formula cells rather than the formula text."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"--- Sheet: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()
