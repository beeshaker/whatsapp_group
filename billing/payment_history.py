"""Unified payment-history helper.

Merges the two independent "money changed hands" tables — recurring
`Payment` (30-day renewal charges) and `GroupUpgradeRequest` (one-off
tier-upgrade charges) — into a single date-sorted list of dicts suitable
for both the PDF statement (billing/pdf.py) and the cross-service JSON
API / admin templates.

No ORM relationship() is used anywhere in this codebase (see models.py);
FKs are bare Integer columns looked up manually via db.get(...). This
helper follows that convention for GroupUpgradeRequest.target_tier_id.
"""
from sqlalchemy import select

from models import GroupTierPrice, GroupUpgradeRequest, Payment

# pdf.py's own convention for a missing/not-applicable table value is a plain
# ASCII hyphen (see `invoice_payment.get("receipt") or "-"` and
# `p.get("receipt") or "-"` in pdf.py) — match it here. This also sidesteps a
# real crash: pdf.py renders with the core "Helvetica" font, which raises
# FPDFUnicodeEncodingException on an em dash ("—") since it isn't in that
# font's supported character set.
_NA = "-"


async def unified_payment_history(db, client_id: int, confirmed_only: bool) -> list[dict]:
    """Return a merged, date-descending list of payment-like events for a client.

    Each dict is shaped as:
        {date, kind, description, phone, amount, receipt, status, period_start, period_end}

    kind is "renewal" (from Payment) or "tier_upgrade" (from GroupUpgradeRequest).
    Tier-upgrade rows have no subscription period, so period_start/period_end
    are rendered as "—".
    """
    payment_stmt = select(Payment).where(Payment.client_id == client_id)
    if confirmed_only:
        payment_stmt = payment_stmt.where(Payment.status == "confirmed")
    payments = (await db.execute(payment_stmt)).scalars().all()

    upgrade_stmt = select(GroupUpgradeRequest).where(GroupUpgradeRequest.client_id == client_id)
    if confirmed_only:
        upgrade_stmt = upgrade_stmt.where(GroupUpgradeRequest.status == "confirmed")
    upgrades = (await db.execute(upgrade_stmt)).scalars().all()

    # Fetch the target tier for each upgrade request's description. No
    # relationship() in this codebase — look up GroupTierPrice manually.
    tier_cache: dict[int, GroupTierPrice | None] = {}
    for req in upgrades:
        if req.target_tier_id not in tier_cache:
            tier_cache[req.target_tier_id] = await db.get(GroupTierPrice, req.target_tier_id)

    history: list[dict] = []

    for p in payments:
        history.append({
            "date": p.initiated_at,
            "kind": "renewal",
            "description": "Group renewal",
            "phone": p.phone,
            "amount": str(p.amount),
            "receipt": p.mpesa_transaction_id,
            "status": p.status,
            "period_start": str(p.period_start),
            "period_end": str(p.period_end),
        })

    for req in upgrades:
        tier = tier_cache.get(req.target_tier_id)
        description = f"Group tier upgrade → {tier.name}" if tier else "Group tier upgrade"
        history.append({
            "date": req.created_at,
            "kind": "tier_upgrade",
            "description": description,
            "phone": req.phone,
            "amount": str(req.amount),
            "receipt": req.mpesa_transaction_id,
            "status": req.status,
            "period_start": _NA,
            "period_end": _NA,
        })

    history.sort(key=lambda h: h["date"], reverse=True)

    # Render dates as strings only after sorting (raw datetimes sort correctly;
    # strings would sort lexicographically, which happens to match here but
    # is fragile — sort on the real datetime, then format).
    for h in history:
        h["date"] = h["date"].strftime("%Y-%m-%d %H:%M")

    return history
