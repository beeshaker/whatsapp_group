import io
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from fpdf import FPDF

_GREEN = (7, 94, 84)
_DARK = (30, 30, 30)
_MUTED = (100, 100, 100)
_LIGHT_BG = (245, 250, 248)
_WHITE = (255, 255, 255)
_BORDER = (220, 230, 228)


class _StatementPDF(FPDF):
    def __init__(self, client_name: str):
        super().__init__()
        self._client_name = client_name

    def header(self):
        self.set_fill_color(*_GREEN)
        self.rect(0, 0, 210, 22, "F")
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*_WHITE)
        self.set_xy(10, 6)
        self.cell(0, 10, "PAYMENT STATEMENT", align="L")
        self.set_font("Helvetica", "", 9)
        self.set_xy(10, 6)
        self.cell(0, 10, f"Whats2Manage Billing", align="R")
        self.ln(22)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_MUTED)
        self.cell(0, 6, f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC  |  Page {self.page_no()}", align="C")


def generate_statement(
    client_name: str,
    client_plan: str,
    client_status: str,
    renewal_date: date,
    payments: list[dict],
    invoice_payment: Optional[dict] = None,
) -> bytes:
    """
    Generate a PDF statement. Returns raw bytes.
    payments: list of dicts with keys: date, phone, amount, receipt, status, period_start, period_end
    invoice_payment: the most recent confirmed payment to highlight as invoice (optional)
    """
    pdf = _StatementPDF(client_name)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # ── Client info block ──────────────────────────────────────────────────
    pdf.set_fill_color(*_LIGHT_BG)
    pdf.set_draw_color(*_BORDER)
    pdf.rect(10, pdf.get_y(), 190, 28, "FD")

    pdf.set_xy(14, pdf.get_y() + 4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*_DARK)
    pdf.cell(90, 6, client_name)

    pdf.set_xy(14, pdf.get_y() + 7)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_MUTED)
    pdf.cell(40, 5, f"Plan: {client_plan.capitalize()}")
    pdf.cell(50, 5, f"Status: {client_status.upper()}")

    pdf.set_xy(14, pdf.get_y() + 6)
    pdf.cell(0, 5, f"Next Renewal: {renewal_date}")

    pdf.ln(14)

    # ── Invoice highlight for most recent confirmed payment ─────────────────
    if invoice_payment:
        pdf.ln(4)
        pdf.set_fill_color(*_GREEN)
        pdf.rect(10, pdf.get_y(), 190, 7, "F")
        pdf.set_xy(14, pdf.get_y() + 1)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*_WHITE)
        pdf.cell(0, 5, "LATEST INVOICE")
        pdf.ln(8)

        pdf.set_fill_color(235, 250, 245)
        pdf.set_draw_color(*_BORDER)
        pdf.rect(10, pdf.get_y(), 190, 34, "FD")
        y0 = pdf.get_y() + 5

        def _inv_row(label: str, value: str, bold: bool = False):
            nonlocal y0
            pdf.set_xy(16, y0)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*_MUTED)
            pdf.cell(50, 5, label)
            pdf.set_font("Helvetica", "B" if bold else "", 9)
            pdf.set_text_color(*_DARK)
            pdf.cell(0, 5, value)
            y0 += 6

        _inv_row("Amount:", f"KES {invoice_payment['amount']}", bold=True)
        _inv_row("M-Pesa Receipt:", invoice_payment.get("receipt") or "-")
        _inv_row("Phone:", invoice_payment.get("phone") or "-")
        _inv_row("Period:", f"{invoice_payment['period_start']} to {invoice_payment['period_end']}")
        _inv_row("Payment Date:", invoice_payment.get("date") or "-")

        pdf.ln(36)

    # ── Payment history table ───────────────────────────────────────────────
    pdf.ln(4)
    pdf.set_fill_color(*_GREEN)
    pdf.rect(10, pdf.get_y(), 190, 7, "F")
    pdf.set_xy(14, pdf.get_y() + 1)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_WHITE)
    pdf.cell(0, 5, "PAYMENT HISTORY")
    pdf.ln(8)

    # Table header
    cols = [("Date", 36), ("Phone", 30), ("Amount", 24), ("Receipt", 36), ("Status", 22), ("Period", 42)]
    pdf.set_fill_color(*_DARK)
    for label, w in cols:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*_WHITE)
        pdf.set_fill_color(*_DARK)
        pdf.cell(w, 6, label, fill=True)
    pdf.ln(6)

    # Table rows
    for i, p in enumerate(payments):
        fill = i % 2 == 0
        pdf.set_fill_color(245, 250, 248) if fill else pdf.set_fill_color(*_WHITE)
        pdf.set_text_color(*_DARK)
        pdf.set_font("Helvetica", "", 7.5)
        row_data = [
            (p.get("date", "")[:16], 36),
            (p.get("phone", ""), 30),
            (f"KES {p.get('amount', '')}", 24),
            (p.get("receipt") or "-", 36),
            (p.get("status", "").upper(), 22),
            (f"{p.get('period_start','')} - {p.get('period_end','')}", 42),
        ]
        for val, w in row_data:
            pdf.cell(w, 5.5, str(val), fill=fill)
        pdf.ln(5.5)

    if not payments:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*_MUTED)
        pdf.cell(0, 10, "No payments on record.", align="C")
        pdf.ln(10)

    # ── Totals ──────────────────────────────────────────────────────────────
    confirmed_total = sum(
        Decimal(str(p["amount"])) for p in payments if p.get("status") == "confirmed"
    )
    pdf.ln(4)
    pdf.set_x(130)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_DARK)
    pdf.cell(40, 6, "Total Paid (confirmed):")
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*_GREEN)
    pdf.cell(0, 6, f"KES {confirmed_total:.2f}")

    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()
