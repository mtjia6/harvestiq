# HarvestIQ — Tax Concepts Notes

*General tax/finance mechanics discussed while building the broker-scoped lot-matching feature. Not tax advice — general information only.*

## 1. Cost basis

What you paid to acquire an asset — purchase price plus fees. It's what you subtract from the sale price to find your taxable gain or loss.

**Example:** Buy 1 BTC at $30,000 + $50 fee → cost basis = $30,050.
- Sell later for $40,000 → gain = $9,950
- Sell later for $25,000 → loss = $5,050 (this is the loss a harvesting engine looks for)

## 2. FIFO (First-In-First-Out)

The default lot-selection method if you don't specify otherwise: when you sell, you're assumed to be selling your *oldest* shares/coins of that symbol first.

**Example:** Buy 10 AAPL Jan 1, buy 10 more Mar 1. Sell 15 → the sale consumes all 10 from the Jan 1 lot plus 5 from the Mar 1 lot, in that order.

## 3. Specific identification

An alternative to FIFO — you (or your broker) explicitly declare which lot a sale comes from (e.g., to pick the highest-cost lot on purpose). Allowed for **both stocks and crypto** — this was never the actual difference between the two asset types (a common misconception). As of 2025, specific ID for crypto must be identified *before* the trade executes, and documented in your own records.

## 4. Wallet-based cost basis for crypto — IRS Rev. Proc. 2024-28

Starting January 1, 2025, the IRS banned "universal" cost-basis pooling for crypto. You can no longer treat all your crypto (across every wallet/exchange) as one basis pool and pick whichever lot's basis you want when you sell. Cost basis must now be tracked **per wallet/account** — a sale from Wallet A can only draw on lots that were actually acquired in Wallet A.

There's a one-time safe-harbor allocation for reallocating pre-2025 basis across wallets (Specific Unit Allocation or Global Allocation), but that's a historical transition step, not ongoing logic.

## 5. Why stocks never had this problem (and where the real gap was)

It's *not* that a broker won't stop you from selling a stock you don't have in that account — it will, exactly like a crypto exchange won't let you sell BTC you don't have there. Execution-time custody is identical for both.

The actual difference: when stock moves between brokers (an ACATS transfer), regulation requires the old broker to send the new broker the cost basis and purchase date, lot by lot — that data is required to travel with the shares. When crypto moves between wallets, it's just a blockchain transaction moving tokens — nothing requires (or even allows) cost-basis data to travel with it. That gap is what let people informally pool basis across wallets pre-2025; Rev. Proc. 2024-28 is a recordkeeping mandate closing it, not a new technical restriction on trading.

## 6. Concrete proof: wrong lot = wrong tax number

Buy 10 AAPL at Broker A on Jan 1 (cost basis $150/share). Buy 10 more AAPL at Broker B on Mar 1 (cost basis $180/share). Sell 10 AAPL *at Broker B* on Jun 1 for $200/share.

- **No broker filter (old behavior):** picks the oldest lot regardless of broker → Broker A's lot, $150 basis → gain = (200−150)×10 = **$500**
- **Broker-scoped (correct):** only looks at Broker B's lots → $180 basis → gain = (200−180)×10 = **$200**

Same sale, same price — a $300 difference in taxable gain, purely from which lot's basis got used.

## 7. Multi-lot sales don't blend cost basis

If a sale is bigger than one lot can cover, it's split across multiple lots — but each lot's *own* cost basis is recorded separately (one realized-gain record per lot consumed), not averaged.

**Example:** Broker B has two lots: 5 shares @ $180 basis, 5 shares @ $190 basis. Sell 8 shares at $200:
- 5 shares from lot 1 → gain = (200−180)×5 = $100
- 3 shares from lot 2 → gain = (200−190)×3 = $30
- Total = $130 (not a blended $185-basis calculation)

## 8. Short-term vs. long-term netting — why "term" matters even though everything nets to one number

The IRS nets in stages, not all at once:
1. All short-term (ST) gains/losses net against each other → one ST total
2. All long-term (LT) gains/losses net against each other → one LT total
3. The two totals combine into one overall net figure (used for the $3,000/year loss deduction against ordinary income; excess carries forward)

**Why this matters:** ST gains are taxed at ordinary income rates (up to 37%); LT gains get preferential rates (0/15/20%). A loss is forced to cancel gains of its *own type* first — you don't get to choose.

**Example:** You have a $1,000 LT gain (taxed at 15% = $150) and a $1,000 ST gain (taxed at 35% = $350). Total tax before harvesting: $500.
- Harvest a $1,000 **LT** loss → cancels the LT gain → new total tax = $350 → **saved $150**
- Harvest a $1,000 **ST** loss instead (same size) → cancels the ST gain → new total tax = $150 → **saved $350**

Same size loss, more than double the savings, depending on which type of gain it's allowed to cancel first.

## 9. What HarvestIQ actually calculates today (known simplification)

`build_harvest_opportunities` computes, per lot, independently:

```python
estimated_tax_savings = -unrealized * tax_rate   # tax_rate depends on that lot's own term
```

This assumes every loss fully applies at its own term's rate — it does **not** check your actual year-to-date realized gains (`RealizedGainDB` exists but isn't queried here) and doesn't run the real netting waterfall from #8. So today's "estimated tax savings" is a per-position ballpark, not a true "given everything else this year, here's what this loss is actually worth" figure. Documented as a known gap, not yet fixed.


Harvesting is only worth it if you actually have gains to offset — if you're in a low bracket, sitting on big loss carryforwards, or have no realized gains coming, you're deferring taxes into a future you may not benefit from. And harvesting isn't free: it's a deferral, not an erasure. Your basis drops, so the tax comes due when you eventually sell, unless the position gets stepped up at death or donated. It also pushes your portfolio to drift from the index you're tracking. Their figures are gross of management fees, which for a direct-indexing SMA can eat a meaningful chunk of that 1%.