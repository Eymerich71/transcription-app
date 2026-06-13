"""Export transcription results to TXT, DOCX, PDF, and Markdown."""

import io
from datetime import datetime
from typing import Any, Dict


def _fmt_time(seconds: float) -> str:
    if seconds is None:
        return "0:00:00"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _speaker(seg: Dict, speaker_map: Dict[str, str]) -> str:
    return speaker_map.get(seg["speaker"], seg["speaker"])


def _meta(t: Dict) -> str:
    return (
        f"Language: {t['language']}  |  Engine: {t['engine']}  |  "
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )


def export_txt(transcription: Dict[str, Any], speaker_map: Dict[str, str]) -> bytes:
    lines = [
        f"TRANSCRIPT: {transcription['filename']}",
        _meta(transcription),
        "=" * 72,
        "",
    ]
    for seg in transcription.get("segments", []):
        ts = f"{_fmt_time(seg['start'])} → {_fmt_time(seg['end'])}"
        lines.append(f"[{_speaker(seg, speaker_map)}]  {ts}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def export_md(transcription: Dict[str, Any], speaker_map: Dict[str, str]) -> bytes:
    lines = [
        f"# Transcript: {transcription['filename']}",
        "",
        _meta(transcription),
        "",
        "---",
        "",
    ]
    for seg in transcription.get("segments", []):
        ts = f"{_fmt_time(seg['start'])} → {_fmt_time(seg['end'])}"
        lines.append(f"**{_speaker(seg, speaker_map)}** `{ts}`")
        lines.append("")
        lines.append(seg["text"])
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def export_docx(transcription: Dict[str, Any], speaker_map: Dict[str, str]) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()

    title = doc.add_heading(f"Transcript: {transcription['filename']}", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT

    meta_para = doc.add_paragraph()
    run = meta_para.add_run(_meta(transcription))
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    doc.add_paragraph()

    for seg in transcription.get("segments", []):
        ts = f"{_fmt_time(seg['start'])} → {_fmt_time(seg['end'])}"
        sp = _speaker(seg, speaker_map)

        header = doc.add_paragraph()
        r_name = header.add_run(sp)
        r_name.bold = True
        r_name.font.color.rgb = RGBColor(0x00, 0x66, 0xCC)
        r_ts = header.add_run(f"  {ts}")
        r_ts.font.size = Pt(8)
        r_ts.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

        doc.add_paragraph(seg["text"])

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


def export_pdf(transcription: Dict[str, Any], speaker_map: Dict[str, str]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TransTitle",
        parent=styles["Heading1"],
        fontSize=15,
        spaceAfter=4,
        alignment=TA_LEFT,
    )
    meta_style = ParagraphStyle(
        "TransMeta",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.gray,
        spaceAfter=10,
    )
    speaker_style = ParagraphStyle(
        "TransSpeaker",
        parent=styles["Normal"],
        fontSize=9,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#0066CC"),
        spaceBefore=10,
        spaceAfter=2,
    )
    text_style = ParagraphStyle(
        "TransText",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        spaceAfter=4,
    )

    def _safe(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = [
        Paragraph(_safe(f"Transcript: {transcription['filename']}"), title_style),
        Paragraph(_safe(_meta(transcription)), meta_style),
        HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey),
        Spacer(1, 10),
    ]

    for seg in transcription.get("segments", []):
        ts = f"{_fmt_time(seg['start'])} → {_fmt_time(seg['end'])}"
        sp = _safe(_speaker(seg, speaker_map))
        story.append(
            Paragraph(
                f'{sp}  <font color="grey" size="7">{ts}</font>',
                speaker_style,
            )
        )
        story.append(Paragraph(_safe(seg["text"]), text_style))

    doc.build(story)
    buf.seek(0)
    return buf.read()
