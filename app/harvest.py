from datetime import date, timedelta
from app.model import SecurityDB, TaxLotDB, TransactionDB

def build_harvest_opportunities(db, short_term_rate, long_term_rate):
    open_lots = (
        db.query(TaxLotDB)
        .filter(TaxLotDB.status == "open")
        .all()
    )

    today = date.today()
    opportunities = []
    securities_by_symbol = {s.symbol: s for s in db.query(SecurityDB).all()}

    for lot in open_lots:
        security = securities_by_symbol.get(lot.symbol)
        if security is None or security.latest_price is None:
            continue

        market_value = lot.quantity_remaining * security.latest_price
        cost_basis = lot.quantity_remaining * lot.cost_basis_per_unit
        unrealized = market_value - cost_basis

        if unrealized >= 0:
            continue

        holding_days = (today - lot.opened_date).days
        if holding_days > 365:
            term = "long"
            tax_rate = long_term_rate
        else:
            term = "short"
            tax_rate = short_term_rate

        estimated_tax_savings = -unrealized * tax_rate

        # Wash-sale rule applies to securities, not crypto (treated as property).
        if security.asset_class == "crypto":
            wash_sale_risk = False
            wash_sale_reason = None
        else:
            window_start = today - timedelta(days=30)
            recent_buy = (
                db.query(TransactionDB)
                .filter(TransactionDB.symbol == lot.symbol)
                .filter(TransactionDB.transaction_type == "BUY")
                .filter(TransactionDB.trade_date >= window_start)
                .first()
            )
            wash_sale_risk = recent_buy is not None
            wash_sale_reason = (
                f"you bought {lot.symbol} on {recent_buy.trade_date}, within the last 30 days"
                if recent_buy else None
            )

        opportunities.append({
            "lot_id": lot.id,
            "symbol": lot.symbol,
            "asset_class": security.asset_class,
            "quantity_remaining": lot.quantity_remaining,
            "lot_broker": lot.broker,
            "cost_basis": cost_basis,
            "market_value": market_value,
            "unrealized_loss": unrealized,
            "term": term,
            "estimated_tax_savings": estimated_tax_savings,
            "wash_sale_risk": wash_sale_risk,
            "wash_sale_reason": wash_sale_reason,
            "recommended": not wash_sale_risk,
        })

    opportunities.sort(key=lambda o: o["estimated_tax_savings"], reverse=True)
    return opportunities
