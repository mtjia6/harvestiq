from datetime import date, timedelta
from app.model import SecurityDB, TaxLotDB, TransactionDB, RealizedGainDB, TaxSettingsDB
from decimal import Decimal

# The IRS caps the net capital loss deductible against ordinary income at
# $3,000 per year (IRC §1211(b)). Anything beyond it carries forward.
ANNUAL_ORDINARY_LOSS_LIMIT = Decimal("3000")


def net_capital_position(net_st, net_lt):
    """Apply the IRS netting waterfall to net short-term and long-term figures.

    Gains are positive, losses negative. Stages (how the IRS actually nets):
      1. ST nets against ST, LT against LT (the caller has already done this —
         the inputs ARE the stage-1 totals).
      2. If one side is a gain and the other a loss, they cross-net.
      3. Whatever loss survives keeps its character (ST or LT), which matters
         because carryforwards come back next year with the same character.

    Returns (taxable_st, taxable_lt, loss_st, loss_lt) — all >= 0.
    taxable_* are gains taxed at their respective rates; loss_* is the
    surviving net loss by character, BEFORE the $3,000 ordinary-income limit.
    """
    zero = Decimal("0")
    if net_st >= 0 and net_lt >= 0:
        return net_st, net_lt, zero, zero
    if net_st >= 0 and net_lt < 0:
        combined = net_st + net_lt
        if combined >= 0:
            # LT loss fully absorbed by ST gain; remainder taxed as ST.
            return combined, zero, zero, zero
        # Loss survives cross-netting; the surviving character is LT.
        return zero, zero, zero, -combined
    if net_st < 0 and net_lt >= 0:
        combined = net_st + net_lt
        if combined >= 0:
            # ST loss fully absorbed by LT gain; remainder taxed as LT.
            return zero, combined, zero, zero
        # Surviving character is ST.
        return zero, zero, -combined, zero
    # Both are losses: nothing to cross-net, both survive with their character.
    return zero, zero, -net_st, -net_lt


def tax_outcome(net_st, net_lt, short_rate, long_rate):
    """Full current-year outcome of a net capital position.

    `liability` can be NEGATIVE: a net loss deducts up to $3,000 against
    ordinary income, which is a real refund valued at the ordinary rate
    (approximated by short_rate — ST gains are taxed as ordinary income).
    Losses beyond the cap produce NO current-year benefit; they become
    carryforward, reported separately so callers never mistake deferred
    value for cash saved this year.
    """
    taxable_st, taxable_lt, loss_st, loss_lt = net_capital_position(net_st, net_lt)

    tax_on_gains = taxable_st * short_rate + taxable_lt * long_rate

    total_loss = loss_st + loss_lt
    ordinary_offset = min(total_loss, ANNUAL_ORDINARY_LOSS_LIMIT)
    # ST losses are consumed first for the ordinary-income deduction
    # (IRC §1212(b) ordering), so the carryforward skews long-term.
    offset_from_st = min(loss_st, ordinary_offset)
    offset_from_lt = ordinary_offset - offset_from_st

    return {
        "tax_on_gains": tax_on_gains,
        "ordinary_offset": ordinary_offset,
        "liability": tax_on_gains - ordinary_offset * short_rate,
        "carryforward_st": loss_st - offset_from_st,
        "carryforward_lt": loss_lt - offset_from_lt,
    }


def tax_owed(net_st, net_lt, short_rate, long_rate):
    """Current-year tax liability (negative = net refund from the $3k offset)."""
    return tax_outcome(net_st, net_lt, short_rate, long_rate)["liability"]


def marginal_harvest_value(loss_amount, term, net_st, net_lt, short_rate, long_rate):
    """What harvesting `loss_amount` (a POSITIVE Decimal) is actually worth,
    given the year's effective net position (realized YTD minus carryforward).

    Returns (current_year_savings, carryforward_generated):
      - current_year_savings: real reduction in this year's tax bill.
      - carryforward_generated: loss that produced no benefit this year and
        rolls forward. Deferred value, NOT savings — never add the two.

    This replaces the old flat `loss * rate` estimate, which claimed full
    savings even in a year already past the $3,000 net-loss cap (where the
    true current-year value of an extra loss is zero).
    """
    before = tax_outcome(net_st, net_lt, short_rate, long_rate)
    if term == "long":
        after = tax_outcome(net_st, net_lt - loss_amount, short_rate, long_rate)
    else:
        after = tax_outcome(net_st - loss_amount, net_lt, short_rate, long_rate)

    savings = before["liability"] - after["liability"]
    carryforward_generated = (
        after["carryforward_st"] + after["carryforward_lt"]
        - before["carryforward_st"] - before["carryforward_lt"]
    )
    return savings, carryforward_generated


def get_carryforwards(db):
    """Prior-year capital-loss carryforwards as POSITIVE loss amounts (st, lt)."""
    settings = db.query(TaxSettingsDB).first()
    if settings is None:
        return Decimal("0"), Decimal("0")
    return settings.carryforward_st, settings.carryforward_lt


def get_effective_net_position(db):
    """This year's net ST/LT position with prior-year carryforwards applied.

    A carryforward re-enters the next year as a loss of the same character,
    so it simply subtracts from the realized YTD totals. This is the position
    every harvest decision must be priced against — a user sitting on a large
    carryforward already has their gains sheltered, and harvesting more adds
    nothing this year.
    """
    realized_st, realized_lt = get_ytd_realized_totals(db)
    carry_st, carry_lt = get_carryforwards(db)
    return realized_st - carry_st, realized_lt - carry_lt


def build_harvest_opportunities(db, short_term_rate, long_term_rate):
    open_lots = (
        db.query(TaxLotDB)
        .filter(TaxLotDB.status == "open")
        .all()
    )

    today = date.today()
    opportunities = []
    securities_by_symbol = {s.symbol: s for s in db.query(SecurityDB).all()}
    net_st, net_lt = get_effective_net_position(db)

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
        else:
            term = "short"

        estimated_tax_savings, carryforward_generated = marginal_harvest_value(
            -unrealized, term, net_st, net_lt, short_term_rate, long_term_rate
        )

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
            "carryforward_generated": carryforward_generated,
            "wash_sale_risk": wash_sale_risk,
            "wash_sale_reason": wash_sale_reason,
            "recommended": not wash_sale_risk,
        })

    # Rank by real this-year savings; among equals, prefer the lot that also
    # banks more carryforward for future years.
    opportunities.sort(
        key=lambda o: (o["estimated_tax_savings"], o["carryforward_generated"]),
        reverse=True,
    )
    return opportunities


def build_harvest_plan(db, short_term_rate, long_term_rate, min_step_savings=Decimal("0")):
    """An ORDERED, executable harvest plan — the portfolio-level answer.

    The per-lot list (`build_harvest_opportunities`) prices every lot against
    the SAME starting position, so its figures are comparable but do not sum:
    harvest the first lot and every other number is stale. This function
    instead simulates actually doing it — greedy sequential:

      1. price every remaining candidate against the CURRENT net position,
      2. take the one with the highest marginal current-year savings,
      3. apply its loss to the running position, and repeat.

    Re-pricing each round matters: once ST gains are exhausted, a short-term
    loss that ranked first can fall behind a long-term one. Greedy is exact
    enough here because marginal value is non-increasing (gains at full rate
    -> the $3,000 ordinary cap -> zero), i.e. diminishing returns.

    Stops when the best remaining lot saves <= `min_step_savings` this year.
    Lots past that line are reported as `carryforward_only` — a legitimate,
    deliberate choice (banking losses), never advertised as savings. Lots
    with wash-sale risk are excluded from the plan and listed separately.
    """
    candidates = build_harvest_opportunities(db, short_term_rate, long_term_rate)
    net_st, net_lt = get_effective_net_position(db)

    remaining = [o for o in candidates if o["recommended"]]
    excluded_wash_sale = [
        {"lot_id": o["lot_id"], "symbol": o["symbol"], "term": o["term"],
         "unrealized_loss": o["unrealized_loss"], "reason": o["wash_sale_reason"]}
        for o in candidates if not o["recommended"]
    ]

    steps = []
    cumulative_savings = Decimal("0")
    cumulative_carryforward = Decimal("0")
    stop_reason = "exhausted_candidates"

    while remaining:
        best = None
        best_value = None
        for o in remaining:
            loss = -Decimal(str(o["unrealized_loss"]))
            value = marginal_harvest_value(
                loss, o["term"], net_st, net_lt, short_term_rate, long_term_rate
            )
            if best is None or value > best_value:
                best, best_value = o, value

        step_savings, step_carryforward = best_value
        if step_savings <= min_step_savings:
            stop_reason = "no_further_current_year_benefit"
            break

        loss = -Decimal(str(best["unrealized_loss"]))
        if best["term"] == "long":
            net_lt -= loss
        else:
            net_st -= loss

        cumulative_savings += step_savings
        cumulative_carryforward += step_carryforward
        steps.append({
            "step": len(steps) + 1,
            "lot_id": best["lot_id"],
            "symbol": best["symbol"],
            "asset_class": best["asset_class"],
            "quantity_remaining": best["quantity_remaining"],
            "lot_broker": best["lot_broker"],
            "term": best["term"],
            "unrealized_loss": best["unrealized_loss"],
            "step_savings": step_savings,
            "step_carryforward": step_carryforward,
            "cumulative_savings": cumulative_savings,
            "cumulative_carryforward": cumulative_carryforward,
        })
        remaining.remove(best)

    # Everything left saves nothing further this year; harvesting it is a
    # pure deferral play. Priced at the post-plan position for honesty.
    carryforward_only = []
    for o in remaining:
        loss = -Decimal(str(o["unrealized_loss"]))
        _, carry = marginal_harvest_value(
            loss, o["term"], net_st, net_lt, short_term_rate, long_term_rate
        )
        carryforward_only.append({
            "lot_id": o["lot_id"],
            "symbol": o["symbol"],
            "term": o["term"],
            "unrealized_loss": o["unrealized_loss"],
            "carryforward_if_harvested": carry,
        })

    return {
        "steps": steps,
        "carryforward_only": carryforward_only,
        "excluded_wash_sale": excluded_wash_sale,
        "total_current_year_savings": cumulative_savings,
        "total_carryforward_generated": cumulative_carryforward,
        "total_harvest_loss": sum(
            (-Decimal(str(s["unrealized_loss"])) for s in steps), Decimal("0")
        ),
        "stop_reason": stop_reason,
    }


def build_portfolio_summary(db, opportunities, plan=None):
    # Pure computation of the dashboard summary. `opportunities` is the cached
    # harvest list (already parsed from Redis by the caller); this function does
    # NOT touch Redis, so harvest.py stays free of celery/redis imports.
    open_lots = db.query(TaxLotDB).filter(TaxLotDB.status == "open").all()
    securities_by_symbol = {s.symbol: s for s in db.query(SecurityDB).all()}

    total_market_value = Decimal("0")
    total_cost_basis = Decimal("0")
    for lot in open_lots:
        cost = lot.quantity_remaining * lot.cost_basis_per_unit
        total_cost_basis += cost
        security = securities_by_symbol.get(lot.symbol)
        if security and security.latest_price is not None:
            total_market_value += lot.quantity_remaining * security.latest_price
        else:
            total_market_value += cost  # no price yet: value at cost

    total_unrealized = total_market_value - total_cost_basis

    realized_short, realized_long = get_ytd_realized_totals(db)
    realized_total = realized_short + realized_long

    carry_st, carry_lt = get_carryforwards(db)
    effective_st = realized_short - carry_st
    effective_lt = realized_long - carry_lt
    effective_total = effective_st + effective_lt

    # Values from the cache are strings; str() is a no-op on a Decimal, so this
    # works whether the data came from Redis (strings) or a fresh compute.
    #
    # Headline savings comes from the PLAN when available: the plan's total is
    # sequentially priced, so it's what harvesting everything recommended
    # actually yields. Summing the per-lot figures (the fallback) overstates —
    # each lot is priced against the same starting position, and the numbers
    # don't survive each other's execution.
    if plan is not None:
        harvestable_savings = Decimal(str(plan["total_current_year_savings"]))
        harvestable_loss = Decimal(str(plan["total_harvest_loss"]))
    else:
        rec = [o for o in opportunities if o["recommended"]]
        harvestable_savings = sum((Decimal(str(o["estimated_tax_savings"])) for o in rec), Decimal("0"))
        harvestable_loss = sum((-Decimal(str(o["unrealized_loss"])) for o in rec), Decimal("0"))

    # How much NEW harvested loss still produces a current-year benefit:
    # enough to zero out any effective net gain, plus whatever headroom is
    # left under the $3,000 ordinary-income deduction. If carryforwards have
    # already pushed the year past the cap, the budget is genuinely zero.
    useful_loss_budget = max(effective_total + ANNUAL_ORDINARY_LOSS_LIMIT, Decimal("0"))

    return {
        "total_market_value": total_market_value,
        "total_cost_basis": total_cost_basis,
        "total_unrealized_gain_loss": total_unrealized,
        "realized_ytd_short": realized_short,
        "realized_ytd_long": realized_long,
        "realized_ytd_total": realized_total,
        "carryforward_short": carry_st,
        "carryforward_long": carry_lt,
        "effective_net_short": effective_st,
        "effective_net_long": effective_lt,
        "effective_net_total": effective_total,
        "harvestable_recommended_loss": harvestable_loss,
        "harvestable_recommended_savings": harvestable_savings,
        "useful_loss_budget": useful_loss_budget,
    }


def get_ytd_realized_totals(db):
    today = date.today()
    year_start = date(today.year, 1, 1)

    realized = (
        db.query(RealizedGainDB)
        .filter(RealizedGainDB.sell_date >= year_start)
        .all()
    )

    realized_short = sum((r.gain_loss for r in realized if r.term == "short"), Decimal("0"))
    realized_long = sum((r.gain_loss for r in realized if r.term == "long"), Decimal("0"))
    return realized_short, realized_long
