"""Every accounting report prints (PDF) and exports to Excel from the same table: a title, a sub line, column
headers, rows and an optional totals row. Amounts stay numbers in Excel (two decimals), so they can be summed there."""

import io
from dataclasses import dataclass, field
from decimal import Decimal

from django.http import HttpResponse
from django.template.loader import render_to_string
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass
class Table:
    title: str
    subtitle: str = ""
    headers: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # lists of str / Decimal / int / None
    totals: list | None = None
    note: str = ""


def _filename(name: str, ext: str) -> str:
    return f'attachment; filename="{name}.{ext}"'


def xlsx_bytes(table: Table) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.sheet_view.rightToLeft = True
    bold = Font(bold=True)
    thin = Side(style="thin", color="BBBBBB")
    ws.append([table.title])
    ws["A1"].font = Font(bold=True, size=14)
    if table.subtitle:
        ws.append([table.subtitle])
    ws.append([])
    ws.append(list(table.headers))
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = bold
        cell.fill = PatternFill("solid", fgColor="EEEEEE")
        cell.border = Border(bottom=thin)
        cell.alignment = Alignment(horizontal="center")
    for row in [*table.rows, *([table.totals] if table.totals else [])]:
        ws.append([float(v) if isinstance(v, Decimal) else v for v in row])
        for cell, value in zip(ws[ws.max_row], row, strict=False):
            if isinstance(value, Decimal):
                cell.number_format = "#,##0.00"
    if table.totals:
        for cell in ws[ws.max_row]:
            cell.font = bold
            cell.border = Border(top=thin)
    for i, header in enumerate(table.headers, start=1):
        cells = [len(str(r[i - 1])) for r in table.rows if i <= len(r) and r[i - 1] is not None]
        longest = max([len(str(header)), *cells])
        ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 48)
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def xlsx_response(table: Table, name: str) -> HttpResponse:
    resp = HttpResponse(xlsx_bytes(table), content_type=XLSX)
    resp["Content-Disposition"] = _filename(name, "xlsx")
    return resp


def _cells(row) -> list[tuple[str, bool]]:
    """(text, is a number) for each cell: amounts with two places and thousands separators, blanks for None."""
    out = []
    for value in row:
        if value is None:
            out.append(("", False))
        elif isinstance(value, Decimal):
            out.append((f"{value:,.2f}", True))
        else:
            out.append((str(value), isinstance(value, int)))
    return out


def pdf_bytes(table: Table, org) -> bytes:
    from apps.documents.pdf import font_css, logo_uri, render_pdf

    org_settings = getattr(org, "settings", None)
    html = render_to_string(
        "pdf/report.html",
        {
            "table": table,
            "rows": [_cells(row) for row in table.rows],
            "totals": _cells(table.totals) if table.totals else None,
            "org": org,
            "settings": org_settings,
            "font_css": font_css(),
            "logo_uri": logo_uri(org_settings),
        },
    )
    return render_pdf(html, format="A4", print_background=True)


def pdf_response(table: Table, org, name: str) -> HttpResponse:
    resp = HttpResponse(pdf_bytes(table, org), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{name}.pdf"'
    return resp


def respond(request, table: Table, name: str):
    """`?format=xlsx` / `?format=pdf` on a report view; None when the page itself was asked for."""
    kind = request.GET.get("format")
    if kind == "xlsx":
        return xlsx_response(table, name)
    if kind == "pdf":
        return pdf_response(table, request.organization, name)
    return None
