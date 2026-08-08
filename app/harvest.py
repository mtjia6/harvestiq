from datetime import date, timedelta
from app.model import SecurityDB, TaxLotDB, TransactionDB, RealizedGainDB, TaxSettingsDB
from decimal import Decimal

# The IRS caps the net capital loss deductible against ordinary income at
# $3,000 per year (IRC §1211(b)). Anything beyond it carries forward.
ANNUAL_ORDINARY_LOSS_LIMIT = Decimal("3000")

# The wash-sale window: 30 days before AND after the sale (61 days total).
WASH_SALE_WINDOW_DAYS = 30


def holding_term(opened_date, as_of):
    """IRS holding-period rule: LONG-term means held MORE than one year,
    measured on the calendar, not as `days > 365`.

    A lot bought Feb 1, 2025 and sold Feb 1, 2026 is exactly one year —
    still SHORT-term; Feb 2 is the first long-term day. The day-count
    version also misclassifies a whole day of boundary cases across leap
    years. (Feb 29 purchases use Feb 28 as the anniversary.)
    """
    try:
        anniversary = opened_date.replace(year=opened_date.year + 1)
    except ValueError:  # Feb 29 in a leap year
        anniversary = opened_date.replace(year=opened_date.year + 1, day=28)
    return "long" if as_of > anniversary else "short"


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

    # ONE query for every symbol's window buys (kills the old per-lot N+1).
    window_start = today - timedelta(days=WASH_SALE_WINDOW_DAYS)
    window_buys_by_symbol = {}
    for tx in (
        db.query(TransactionDB)
        .filter(TransactionDB.transaction_type == "BUY")
        .filter(TransactionDB.trade_date >= window_start)
        .all()
    ):
        window_buys_by_symbol.setdefault(tx.symbol, []).append(tx)

    for lot in open_lots:
        security = securities_by_symbol.get(lot.symbol)
        if security is None or security.latest_price is None:
            continue

        market_value = lot.quantity_remaining * security.latest_price
        cost_basis = lot.quantity_remaining * lot.cost_basis_per_unit
        unrealized = market_value - cost_basis

        if unrealized >= 0:
            continue

        term = holding_term(lot.opened_date, today)

        estimated_tax_savings, carryforward_generated = marginal_harvest_value(
            -unrealized, term, net_st, net_lt, short_term_rate, long_term_rate
        )

        # Wash-sale rule applies to securities, not crypto (treated as property).
        if security.asset_class == "crypto":
            wash_sale_risk = False
            wash_sale_reason = None
            safe_to_rebuy_after = None
        else:
            # A wash sale needs REPLACEMENT shares: shares bought inside the
            # window that you'd still hold AFTER the sale. If this lot itself
            # is the recent buy and the sale liquidates it entirely, nothing
            # is "washed" — the old any-recent-buy check wrongly vetoed
            # exactly those sharp fresh-drawdown lots, a harvester's best
            # candidates.
            window_buys = window_buys_by_symbol.get(lot.symbol, [])
            window_buy_qty = sum((tx.quantity for tx in window_buys), Decimal("0"))
            own_window_qty = (
                lot.quantity_remaining if lot.opened_date >= window_start else Decimal("0")
            )
            replacement_qty = window_buy_qty - own_window_qty

            wash_sale_risk = replacement_qty > 0
            if wash_sale_risk:
                buy_dates = ", ".join(str(tx.trade_date) for tx in window_buys)
                wash_sale_reason = (
                    f"{replacement_qty.normalize()} shares of {lot.symbol} bought within the last "
                    f"{WASH_SALE_WINDOW_DAYS} days (on {buy_dates}) would remain after this sale, "
                    f"so the loss would be disallowed against them"
                )
            else:
                wash_sale_reason = None
            # Selling today starts the forward half of the window: a rebuy on
            # or before this date disallows the harvested loss.
            safe_to_rebuy_after = today + timedelta(days=WASH_SALE_WINDOW_DAYS)

        # DRIP guard: an automatic dividend reinvestment is a BUY the
        # wash-sale rule counts, and the engine can't stop the broker from
        # making it. If reinvestment is on and a dividend lands inside the
        # ±30-day window around a sale today, warn BEFORE recommending the
        # sale — "I sold everything, why did the IRS still deny my loss?"
        # is almost always this.
        drip_risk = False
        drip_warning = None
        if (
            security.asset_class != "crypto"
            and security.drip_enabled
            and security.next_dividend_date is not None
        ):
            days_away = (security.next_dividend_date - today).days
            if -WASH_SALE_WINDOW_DAYS <= days_away <= WASH_SALE_WINDOW_DAYS:
                drip_risk = True
                if days_away >= 0:
                    drip_warning = (
                        f"dividend expected on {security.next_dividend_date} with automatic "
                        f"reinvestment ON — the reinvestment would count as a purchase inside "
                        f"the wash-sale window and disallow part of this loss. Turn off "
                        f"dividend reinvestment for {lot.symbol} first, then sell."
                    )
                else:
                    drip_warning = (
                        f"a dividend was paid on {security.next_dividend_date} with automatic "
                        f"reinvestment ON — reinvested shares from it count as a purchase "
                        f"inside the wash-sale window. Check your broker for the reinvestment "
                        f"and record it, or wait until {WASH_SALE_WINDOW_DAYS + 1} days after "
                        f"that date to sell."
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
            "safe_to_rebuy_after": safe_to_rebuy_after,
            "drip_risk": drip_risk,
            "drip_warning": drip_warning,
            # DRIP risk doesn't veto the lot — the user can fix it (turn off
            # reinvestment) and then sell. It's an action-required warning,
            # unlike wash_sale_risk which reflects purchases already made.
            "recommended": not wash_sale_risk,
        })

    # Rank by real this-year savings; among equals, prefer the lot that also
    # banks more carryforward for future years.
    opportunities.sort(
        key=lambda o: (o["estimated_tax_savings"], o["carryforward_generated"]),
        reverse=True,
    )
    return opportunities


def _transfer_disallowed_loss(db, realized, replacement_lot, sell_date):
    """Move as much of `realized`'s not-yet-disallowed loss as `replacement_lot`
    can absorb. Implements the two legs of IRC §1091:

      - the loss is DISALLOWED for tax purposes (realized.gain_loss moves
        toward zero; the amount is recorded in disallowed_wash_loss), and
      - it doesn't vanish: it's added to the replacement lot's basis, and the
        replacement's holding period is tacked back by the sold lot's holding
        period, so the deferred loss keeps its seniority.

    Simplification (documented): the basis bump and the tacked date are
    spread across the replacement lot's remaining shares rather than
    splitting the lot. Total basis is exactly right; per-share attribution
    is approximate when a lot is only partially a replacement.
    """
    economic_loss = realized.cost_basis - realized.proceeds  # positive = loss
    remaining = economic_loss - realized.disallowed_wash_loss
    capacity = replacement_lot.quantity_remaining - replacement_lot.wash_replacement_used
    if remaining <= 0 or capacity <= 0:
        return Decimal("0")

    per_share_loss = economic_loss / realized.quantity_sold
    shares = min(remaining / per_share_loss, capacity)
    amount = shares * per_share_loss

    realized.disallowed_wash_loss += amount
    realized.gain_loss += amount  # gain_loss is the TAX-reportable figure

    replacement_lot.cost_basis_per_unit += amount / replacement_lot.quantity_remaining
    replacement_lot.wash_replacement_used += shares

    # Holding-period tacking: the replacement inherits the sold shares' age.
    loss_lot = db.query(TaxLotDB).filter(TaxLotDB.id == realized.lot_id).first()
    if loss_lot is not None:
        held = sell_date - loss_lot.opened_date
        replacement_lot.opened_date = replacement_lot.opened_date - held

    return amount


def apply_wash_sale_on_sell(db, symbol, sell_date, realized_records):
    """After a SELL: disallow losses to the extent replacement shares (bought
    inside the ±30-day window, still held after the sale) exist right now."""
    window_start = sell_date - timedelta(days=WASH_SALE_WINDOW_DAYS)
    window_end = sell_date + timedelta(days=WASH_SALE_WINDOW_DAYS)
    replacement_lots = (
        db.query(TaxLotDB)
        .filter(TaxLotDB.symbol == symbol)
        .filter(TaxLotDB.status == "open")
        .filter(TaxLotDB.opened_date >= window_start)
        .filter(TaxLotDB.opened_date <= window_end)
        .order_by(TaxLotDB.opened_date)
        .all()
    )
    for realized in realized_records:
        if realized.cost_basis <= realized.proceeds:
            continue  # not a loss
        for lot in replacement_lots:
            _transfer_disallowed_loss(db, realized, lot, sell_date)


def apply_wash_sale_on_buy(db, symbol, new_lot, buy_date):
    """After a BUY: the forward half of the window. A purchase within 30 days
    of an earlier realized loss makes the new lot a replacement — the loss is
    retroactively disallowed and rolled into this lot's basis. (Without this
    hook, only sell-time detection would run and the books would drift from
    what the IRS computes.)"""
    window_start = buy_date - timedelta(days=WASH_SALE_WINDOW_DAYS)
    window_end = buy_date + timedelta(days=WASH_SALE_WINDOW_DAYS)
    losses = (
        db.query(RealizedGainDB)
        .filter(RealizedGainDB.symbol == symbol)
        .filter(RealizedGainDB.sell_date >= window_start)
        .filter(RealizedGainDB.sell_date <= window_end)
        .filter(RealizedGainDB.cost_basis > RealizedGainDB.proceeds)
        .order_by(RealizedGainDB.sell_date)
        .all()
    )
    for realized in losses:
        _transfer_disallowed_loss(db, realized, new_lot, realized.sell_date)


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
            "drip_risk": best["drip_risk"],
            "drip_warning": best["drip_warning"],
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
