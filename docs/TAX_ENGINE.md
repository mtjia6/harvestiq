# HarvestIQ — The Tax Engine

The domain logic behind HarvestIQ: what it computes and why. All of it lives in `app/harvest.py`, which is pure Python with no web, queue, or cache imports. For the system architecture, see the [main README](../README.md).

> ⚠️ **Educational tool, not tax advice.** The harvesting *logic* is rigorous; the *tax rate model* is a stated simplification you supply. Read [Tax-model limitations](#tax-model-limitations) before trusting a dollar figure.

---

## What is tax-loss harvesting?

When you sell an investment for less than you paid, that **capital loss** can offset your **capital gains** (and up to $3,000 of ordinary income per year), lowering your tax bill. Harvesting is the deliberate practice of selling losing positions to capture those losses while staying invested.

The catch is the details: your exact cost basis per lot, your holding period, how much tax a sale actually saves *given everything else you've already realized this year*, and whether the [**wash-sale rule**](#wash-sales-are-enforced-not-flagged) — which disallows your loss if you rebuy a substantially identical security within 30 days either side of the sale — would nullify the benefit.

**The part most tools skip:** harvesting is mostly a *deferral*, not a saving. Selling at a loss and rebuying resets your cost basis **downward** by exactly the loss you harvested, so the eventual sale owes more. If your future tax rate matches today's, the lifetime benefit is close to zero — you've taken an interest-free loan, not free money. And if the gain you're offsetting would have been taxed at 0% anyway, harvesting actively *costs* you.

HarvestIQ shows you both sides of that trade on every recommendation.

**Worked example.** You own 100 AAPL bought at $200 ($20,000 basis); it's now $150 ($15,000). You also have an $8,000 realized short-term gain this year. Sell the AAPL → realize a $5,000 loss → it cancels $5,000 of that gain → at a 37% rate you save $1,850 now. But your replacement position carries a $15,000 basis instead of $20,000, so you'll owe roughly $750 more when you eventually sell (at a 15% long-term rate). Net lifetime benefit ≈ $1,100, not $1,850. HarvestIQ reports all three numbers.

---

## The tax engine

This is the part worth reading. Most consumer tools stop at "this position is down, sell it."

### Real netting, not a flat multiply

Losses don't just get multiplied by a rate. They cancel gains of their **own character first** (short-term against short-term, long-term against long-term), then cross-net, then hit the **$3,000 ordinary-income cap**, then roll forward as carryforwards that keep their character. `marginal_harvest_value` prices every loss against your *effective* position — realized year-to-date, minus prior-year carryforwards — and returns current-year savings and carryforward generated as **two separate numbers**, never conflated.

This matters most in a year you're already at a net loss: an extra harvest saves you **$0**, and a flat-rate engine will happily report thousands in phantom savings.

### The plan is sequenced, not a ranked list

`GET /harvest-opportunities` prices each lot against the same starting position, so the figures are comparable but **do not sum** — harvest the first one and every other number is stale.

`GET /harvest-plan` simulates actually doing it: price every remaining candidate against the *current* net position, take the best, apply it, repeat. Once short-term gains are exhausted, a short-term loss that ranked first can legitimately fall behind a long-term one, and the plan sees that.

**What "best" means.** Each candidate is scored on one number:

```
score = current_year_savings + (expected_future_rate × carryforward_discount) × carryforward_generated
```

Current-year savings are cash. Carryforward is a claim on a future year, so it is worth the rate you expect to pay then, marked down by how much you trust that expectation — which is the same `carryforward_discount` dial described below. Set it to 0 and the plan optimizes purely for cash this year; set it to 1 and a dollar banked counts as much as a dollar saved. The dial therefore does two jobs: it sizes each sell, and it sets the exchange rate the ranking uses.

**Why this formula and not a simpler one.** Ranking by *size of loss* is the obvious choice and the wrong one — the biggest loss is not the biggest benefit, and in a year where your gains are already used up it buys nothing at all. Ranking by *current-year savings alone* is right until it isn't: once the useful-loss budget is spent, every remaining lot scores exactly zero and the order goes arbitrary — precisely when the user still has real choices to make between lots. The second term exists to keep the ranking meaningful past that point.

Adding the two terms requires a price, because they aren't the same thing: one is cash this April, the other is a claim on a future return. The only defensible conversion is the tax rate you expect to pay when you actually use the carryforward, marked down by how much you believe your own forecast — which is exactly `expected_future_rate × carryforward_discount`. HarvestIQ makes you supply both rather than inferring a discount from an assumed horizon and rate path: fewer hidden assumptions, and every number in the plan can be traced back to a dial you set.

The sum is linear because both terms are already denominated in dollars of tax. That's a real modeling choice, not a neutral one — it assumes you're indifferent between a certain dollar now and a dollar of expected value later, once the dial has priced the uncertainty. If you're more risk-averse than that, lower the dial; that's what it's for.

**What the score deliberately leaves out** is the future tax cost of the basis reduction. Every harvest lowers your replacement basis by exactly the loss taken, so the eventual sale owes `loss × future_rate` more — and HarvestIQ computes that for every step and shows it as `step_future_tax_cost` and `step_net_benefit`. It just doesn't *rank* on it. Ranking on net benefit sounds more honest and behaves worse: in the common same-rate case net benefit is near zero for every candidate, so the ordering degenerates again for the same reason savings-only does. The thrift tiebreaker below covers the practical concern — don't spend more basis than the benefit needed — while whether the deferral is worth taking at all stays a judgment call the user makes with the three numbers in front of them, rather than one the ranking quietly makes for them.

**On greediness.** Taking the best step, applying it, and re-scoring is a heuristic, not a proof of optimality — the plan isn't searched over all orderings. What makes it defensible is that the value of a marginal dollar of harvested loss is non-increasing: each dollar consumes gains of its own character first, then cross-nets, then hits the $3,000 cap, then falls to carryforward. Re-scoring after every step is the part that matters, and it's the part a ranked list can't do.

**Ties break toward thrift.** Equal scores prefer the lot that spends **less** unrealized loss, then a whole-lot sell over a partial one. This is the scarce-resource principle applied to ordering: when two harvests buy the same benefit, take the one that burns less of a resource you can never get back — and, all else equal, the one that's simpler to execute.

**Where it stops.** The loop halts as soon as the best remaining score falls to `min_step_savings` (0 by default). Everything past that line still holds losses, but harvesting them buys nothing this year — so they're returned separately as `carryforward_only`, priced against the *post-plan* position, rather than padded into the plan as savings. Deferring those is a real choice, and lots with a seasoning deadline are flagged there specifically, because those are exactly the ones being left for next year.

Note the two endpoints rank by different rules on purpose: `/harvest-opportunities` sorts by current-year savings then carryforward against a **static** position, while `/harvest-plan` re-scores every survivor against the position as it moves.

### Sells are sized, not all-or-nothing

Unrealized loss is a scarce, non-renewable resource — harvesting spends it permanently. Selling a $40,000 lot to capture $4,000 of benefit burns ten times what the job needed.

The `carryforward_discount` dial (0–1) is how you tell the engine what banked carryforward is worth to you, and it moves the sell size continuously:

```
sized = useful_loss_budget + (full_lot_loss − useful_loss_budget) × discount
```

At **0** ("no gains coming") sells are sized to land exactly on the remaining useful-loss budget. At **1** ("big gain next year") it banks whole lots. At **0.3** it takes the budget plus 30% of the excess.

### Which lot you sell is the whole trade

**What a lot is.** Every purchase of a security is its own **tax lot**, with its own cost basis and its own purchase date. If you bought AAPL three times, you don't own "100 AAPL at an average price" for tax purposes — you own three separate lots, and a sale of 40 shares has to be attributed to specific ones. Which ones you pick determines the gain or loss you realize and whether it's short- or long-term. Same trade, same proceeds, different tax bill.

**Why the default is the wrong one.** Absent an instruction, the IRS treats a sale as **first-in, first-out** — the oldest shares go first. In a position you've been adding to over time, the oldest shares are usually the cheapest, which means FIFO systematically sells your largest gain and your smallest loss. That is precisely backwards for harvesting: the default method is the one most hostile to the thing you're trying to do. Choosing a lot is not a micro-optimization on top of harvesting; **it is** harvesting.

**Three methods**, on any sell via `lot_selection`:

- `fifo` — oldest lot first. The IRS default, and what you get if you say nothing.
- `hifo` — highest cost basis first, ties broken by age. Realizes the largest loss (or the smallest gain) per share sold; the sane default when you aren't targeting a particular lot.
- `specific` — consume exactly the lots you name in `lot_ids`, in the order you give them. This is **specific identification**, the method the regulations actually contemplate, and the only one that lets a plan be executed as designed.

That last one is why the plan is executable rather than advisory. Each step of `GET /harvest-plan` names a lot id and a quantity, and the dashboard's execute button fires that lot id straight back into `POST /transactions`. Reproducing a recommendation by hand in a broker interface usually falls back to FIFO — so you'd realize a different taxable result from the one that was valued. The advice would be correct and the outcome still wrong.

**What the engine enforces.** Candidate lots are always filtered to *open* lots of that symbol **in that wallet** — sells are broker-scoped, so a lot at Fidelity can't satisfy a sale at Schwab. Specific ids are validated on all four counts (exists, right symbol, right wallet, still open) and rejected with a 400 rather than silently falling back. An oversell is refused before anything is written. And ordering always reads the real `opened_date`, never the wash-tacked holding-period date — tacking changes how a loss is *classified*, not which shares are physically oldest.

### Wash sales are enforced, not flagged

**The rule, plainly.** If you sell a security at a loss and buy the same — or a "substantially identical" — security within **30 days before or 30 days after** that sale, the IRS disallows the loss (IRC §1091). It is a 61-day window centered on the sale day, and buying *first* and selling *second* triggers it just as buying after does. The loss is not deducted this year, so the harvest you were counting on produces nothing.

The loss is usually deferred rather than destroyed. The disallowed amount is added to the **cost basis of the replacement shares**, and the holding period of the shares you sold is **tacked onto** them — so you recover the deduction when you eventually sell the replacement, and you keep credit for the time already served toward long-term treatment. The exceptions are the ones that hurt: if the replacement is bought in an IRA or 401k there is no taxable basis to adjust and the loss is **gone for good** (Rev. Rul. 2008-5).

Three details that catch people. The rule follows the **taxpayer**, not the account — your spouse's account and your IRA count, and no broker can see them, so no broker's 1099-B is complete. **Any** purchase counts, including ones you did not consciously make: a dividend reinvestment, an automated recurring buy, an option exercise. And it applies **only to losses** — selling at a gain and rebuying immediately is fine.

**How HarvestIQ handles it.** Detection runs *before* the sale — replacement shares are computed from open lots inside the ±30-day window, net of shares that already absorbed a previous disallowed loss (a share can only block one harvest, never two). Fully liquidating a fresh buy is correctly **not** flagged.

Enforcement runs at transaction time, in both directions. The disallowed loss doesn't vanish: it attaches to the replacement **shares** — basis up, holding period tacked back. Partial washes **split the replacement lot**, because Treas. Reg. §1.1091-1 matches share-by-share: sell 40 and rebuy 100 and the loss attaches to 40 of those shares, not pro-rata across all 100.

**Across accounts**, which is the gap nothing mainstream covers. The wash-sale rule applies to the *taxpayer*; each broker only sees its own account. Register watch-only external accounts (a spouse's taxable account, an IRA, a 401k) and their purchases count toward the window. When the replacement lands in a retirement account the loss is **permanently destroyed** rather than deferred — there's no basis to adjust (Rev. Rul. 2008-5) — and HarvestIQ says so.

Crypto is exempt (property, not a security, per Notice 2014-21).

### What you buy back

Harvesting is supposed to be **exposure-neutral**: you sell the loser, put the same dollars into something that behaves like it, and end the day with the same market position and a tax deduction you didn't have this morning. That assumption is doing real work — it's what makes "your basis drops by exactly the loss you harvested" true, and it's why harvesting can be described as a deferral rather than a bet. Reinvest less than the proceeds and you haven't just harvested a loss, you've also reduced your market exposure, and any return those dollars would have earned is a cost that appears in none of the tax numbers.

**What the replacement's basis is.** Whatever you pay for it — nothing carries over from the lot you sold. Sell 100 AAPL for $15,000 and put all $15,000 into the replacement and the new basis is $15,000 (fees included); put in $10,000 and it's $10,000. Proceeds and basis coincide only because you reinvested in full, not because of any rule connecting them. The one exception is the wash sale, where the replacement's basis is what you paid **plus** the disallowed loss — which is exactly the mechanism that hands the deduction back to you later.

**How suggestions work today — honestly, this is a stub.** `GET /replacement-suggestions` takes each harvest opportunity, reads the sold security's `sector`, and returns every other security in the system sharing that sector. That's the entire algorithm. It is a starting point for a human, not an answer:

- **No "substantially identical" screening.** The suggestions are same-sector-different-symbol. Whether a given pair is substantially identical is the actual legal question, and the IRS has never defined the term — two S&P 500 index funds from different issuers are the canonical unresolved case. Nobody has solved this; HarvestIQ does not pretend to.
- **No correlation or tracking-error screen.** Same sector is a crude proxy for "behaves like the thing I sold." A replacement that diverges from the original is a tracking bet you didn't intend to make.
- **No sizing.** The endpoint returns symbols, never a dollar amount or share count, and never references the proceeds of the sale it's replacing. The exposure-neutrality the tax math assumes is left entirely to you, and nothing in the system verifies you maintained it.
- **No window check.** Suggestions are not cross-referenced against the ±30-day window — that enforcement happens at transaction time, when you actually record the buy.

Only the universe of securities already known to HarvestIQ is searched, so the quality of the suggestions is bounded by what you've told it about.

### Warnings for the traps that actually bite people

- **DRIP guard** — automatic dividend reinvestment is a purchase the wash-sale rule counts, and it's the single most common way people lose a harvested loss. If a distribution falls inside the ±30-day window, you get told to turn reinvestment off *before* selling.
- **Seasoning deadline** — a short-term loss that crosses its one-year anniversary becomes long-term and is usually worth less. HarvestIQ prices the difference and only warns when waiting actually costs money. (It often doesn't: with only short-term gains on the books, a long-term loss cross-nets into them at the short rate anyway.)
- **Harvest capacity** — how much harvestable loss is *left*, and how far each position must fall before it offers anything. Repeated harvesting ratchets basis down until a portfolio is ossified: still holding losses on paper, but nothing takeable without a serious drawdown.

### Per-wallet cost basis

Universal pooled basis for crypto is dead. Rev. Proc. 2024-28 and Treas. Reg. §1.1012-1(j) made wallet-by-wallet basis **mandatory** for tax years beginning after 2024-12-31. HarvestIQ enforces it end-to-end: a sale can only draw on lots actually held in that wallet, and `POST /transfers` moves lots between wallets carrying basis and dates without realizing anything.

---

## Tax-model limitations

The honest split: **the harvesting logic is rigorous, the tax rate model is a stated simplification.** The machinery for producing a defensible answer is there and it's fed a rate you type in. That rate can be wrong by a factor of two or three in either direction, and everything downstream inherits the error. Treat `estimated_tax_savings` as *directionally* sound and the dollar figure as indicative.

**Rate modelling (the big one):**

- **Flat rates, no brackets.** You supply a short- and long-term rate. There's no bracket stacking and no 0% / 15% / 20% breakpoints. If your income puts you in the **0% long-term band**, HarvestIQ will tell you a long-term harvest saves 15% when it saves nothing — and it's worse than nothing, because you've ratcheted basis down against a future taxable gain.
- **No NIIT.** The 3.8% surtax isn't modelled, and its treatment of capital losses is unintuitive (net losses count against net investment income only up to the same $3,000).
- **No §1(h) rate groups.** Collectibles at 28% (including physical-metal ETFs like GLD and SLV) and unrecaptured §1250 at 25% are treated as ordinary long-term.
- **No AMT, no state tax.** Pennsylvania in particular has no carryforward, no $3,000 offset, no ST/LT distinction — and the federal wash-sale rules don't apply for PA personal income tax. New Jersey has no carryforward either.
- **No cliff modelling.** IRMAA, ACA subsidy cliffs and Social Security's provisional-income phase-in all interact with capital losses and none are represented.

**Scope and mechanics:**

- **Carryforward has no present value or mortality discount.** It's reported honestly as separate from current-year savings, but a dollar banked for a user with no foreseeable gains is worth much less than face — and unused carryforwards expire at death (Rev. Rul. 74-175).
- **"Substantially identical" is not solved.** Replacement suggestions are same-sector-different-symbol, with no correlation or tracking-error screening. Nobody has solved this; the IRS has never defined the term.
- **The mandatory-use trap isn't caught.** §1211(b) requires the loss to be used to the maximum extent in the current year — there's no election to defer. A taxpayer with no gains and income below the standard deduction destroys $3,000 of carryforward for nothing, and HarvestIQ will still recommend the harvest.
- **Some hidden costs aren't modelled:** the §852(b)(4)(A) six-month rule (a capital gain distribution recharacterizes a short-term loss as long-term), the 61-day qualified-dividend rule under §246(c), and return-of-capital distributions reducing basis under §301(c)(2).
- **Special regimes invalidate the engine entirely** for some users: a §475(f) mark-to-market trader is exempt from §1091 and has no $3,000 cap; §1092 straddles and §1259 constructive sales aren't handled.
- **Crypto:** wallet-by-wallet basis is enforced, but the pre-2025 safe-harbor allocation isn't modelled, and crypto ETPs (which *are* securities and *do* get §1091) aren't yet distinguished from spot crypto in the schema.
- **Transfer fees** aren't added to basis; currency is stored but unused in P&L.
