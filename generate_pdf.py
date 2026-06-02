"""
CBG Manager — генератор Checks Sheet PDF
"""

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
import io


def generate_checks_pdf(order_num: str, client_name: str, model: str, rows: list) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    header_left_style = ParagraphStyle("header_left", fontSize=11, fontName="Helvetica-Bold", alignment=TA_LEFT)
    header_center_style = ParagraphStyle("header_center", fontSize=11, fontName="Helvetica-Bold", alignment=TA_CENTER)
    header_right_style = ParagraphStyle("header_right", fontSize=11, fontName="Helvetica-Bold", alignment=TA_RIGHT)

    story = []

    header_data = [[
        Paragraph(model, header_left_style),
        Paragraph(f"{order_num} - {client_name}", header_center_style),
        Paragraph("CHECKS SHEET", header_right_style),
    ]]
    header_table = Table(header_data, colWidths=[2.2*inch, 3.1*inch, 2.2*inch])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))

    col_headers = ["ITEM", "SIZE/CODE", "QUANT.", "CRATE #", "✓", "COMMENTS"]
    col_widths = [2.2*inch, 1.4*inch, 0.65*inch, 0.75*inch, 0.4*inch, 2.1*inch]

    table_data = [col_headers]
    for row in rows:
        table_data.append([
            str(row.get("item", "")),
            str(row.get("size_code", "")),
            str(row.get("quant", "")),
            "", "", "",
        ])

    table_style = TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#222222")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9),
        ("ALIGN", (0,0), (-1,0), "CENTER"),
        ("VALIGN", (0,0), (-1,0), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,0), 5),
        ("BOTTOMPADDING", (0,0), (-1,0), 5),
        ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE", (0,1), (-1,-1), 8.5),
        ("VALIGN", (0,1), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,1), (-1,-1), 3),
        ("BOTTOMPADDING", (0,1), (-1,-1), 3),
        ("ALIGN", (2,1), (2,-1), "CENTER"),
        ("ALIGN", (4,1), (4,-1), "CENTER"),
        *[("BACKGROUND", (0,i), (-1,i), colors.HexColor("#f2f2f2")) for i in range(2, len(table_data), 2)],
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#aaaaaa")),
        ("LINEBELOW", (0,0), (-1,0), 1.5, colors.HexColor("#000000")),
    ])

    parts_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    parts_table.setStyle(table_style)
    story.append(parts_table)
    doc.build(story)
    buffer.seek(0)
    return buffer.read()

