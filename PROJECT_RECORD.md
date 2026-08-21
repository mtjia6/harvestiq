# Project Career Record — HarvestIQ

*My permanent record of what I built on HarvestIQ: a tax-loss harvesting engine whose point is that it encodes hard tax law correctly. Wash-sale enforcement under §1091 with basis adjustment and holding-period tacking, partial washes that split the replacement lot rather than smearing basis across clean shares, a one-disallowed-loss-per-share invariant, cross-account washes through watch-only accounts, a dividend reinvestment guard, per-wallet basis under Rev. Proc. 2024-28 with basis-preserving transfers, and the full netting waterfall. Three parts:*

- **Deliverable 1: Product & capabilities.** How HarvestIQ works, the law it encodes, and every capability: what it does, the problem it solves, and the mechanism behind it.
- **Deliverable 2: Résumé & application bank.** The same work compressed into metric-driven bullets.
- **Deliverable 3: Build logs (Q&A).** Problem-to-solution logs covering the systems and engineering side.

**How it was built (authorship note, kept honest for interviews).** AI-assisted pair programming. An AI tutor explained concepts and proposed code; I made every product and architecture decision, wrote and edited the code into the real files, ran the tests, and drove the debugging. I also did the domain research and **audited the AI-generated tax logic against primary sources**, meaning the Internal Revenue Code, Treasury Regulations, and IRS rulings. That audit found five substantive defects in code that looked correct and passed its own test suite. Fixing them is most of what the engine is now.

---

# DELIVERABLE 1 — PRODUCT & CAPABILITIES

## What HarvestIQ is

**Tax-loss harvesting is the practice of selling underperforming investments at a loss to offset capital gains realized from profitable assets.** By matching losses against gains, investors lower their net taxable income and reduce the tax bill owed on their portfolios. Losses beyond gains offset up to $3,000 of ordinary income annually, and the remainder carries forward indefinitely.

**HarvestIQ is a harvesting engine that decides which lots to sell, in what order, and in what size.** You give it your transaction history. It tracks every holding at the tax-lot level across all your brokerage accounts and wallets, values them against live prices, and produces an ordered plan you can execute directly.

### What the project actually is

**The point of this project is that it encodes hard tax law correctly.** Not a heuristic that finds losing positions and suggests selling them, which takes an afternoon, but a real implementation of the specific rules that govern whether a harvested loss is allowed, what it is worth, and what happens to it when it is not allowed.

The rules that are actually implemented, and what each one demands:

**Wash-sale enforcement under IRC §1091, with basis adjustment and holding-period tacking.** The rule is not a warning. When it fires, the disallowed loss moves into the cost basis of the replacement shares under §1091(d), and the holding period of the shares you sold is added to theirs under §1223(3). Both consequences are applied to the ledger at transaction time. A tool that only flags the risk leaves you holding a position whose recorded basis is understated by exactly the disallowed amount, and that error sits there until you sell.

**Partial washes split the replacement lot rather than smearing basis across clean shares.** Treas. Reg. §1.1091-1 matches shares one for one in order of acquisition. Sell 40 and repurchase 100 and the whole disallowed loss attaches to 40 of those shares, not thinly across all of them. Smearing keeps the aggregate figures right, which is exactly why the bug is invisible, while making every individual share's basis and classification wrong. The cost surfaces on the next harvest, where a correct engine reports a $300 short-term loss plus a $600 long-term loss worth about $201 and the smeared version reports $900 entirely long-term, worth about $135.

**A one-disallowed-loss-per-share invariant.** The same regulation does not let a replacement share absorb a second disallowed loss. Without the invariant the engine over-reports exposure and suppresses harvests you were legally entitled to take, which is a cost delivered through a recommendation that never appears.

**Cross-account washes through watch-only external accounts.** §1091 applies to the taxpayer across every account held, while each broker sees only the account it custodies. Nothing in the industry reconciles those two facts. A TurboTax specialist's verbatim answer on handling this across two brokers is *"No. and neither does professional software used by pros. You'll have to do it manually."* HarvestIQ counts purchases in a spouse's account, an IRA or a 401(k) toward the window, and treats the retirement case correctly as permanent destruction of the loss under Rev. Rul. 2008-5, because there is no basis in a tax-advantaged account to adjust.

**A dividend reinvestment guard.** An automatically reinvested dividend is an acquisition, and §1091 counts it. This is the single highest-volume way people lose a harvested loss, and it cannot be detected after the fact because the engine cannot see broker settings. The warning has to arrive before the sale.

**Per-wallet cost basis under Rev. Proc. 2024-28, with basis-preserving transfers.** Pooling crypto basis across wallets became prohibited for tax years beginning after 31 December 2024. Sales are scoped to the account holding the lots, and moving assets between your own accounts carries the basis, the real acquisition date, and any inherited holding period across without realizing anything.

**The full netting waterfall.** Short-term against short-term, long-term against long-term, cross-netting of whatever survives, the $3,000 ordinary-income limitation under §1211(b), and carryforwards that retain their character into the following year under §1212(b), where that character determines what they are allowed to offset.

Most tools implement none of this. They multiply the loss by a tax rate and call the product the saving, which ignores the gains you have already realized, ignores that a loss must offset gains of its own classification first, and ignores that the benefit stops entirely past the $3,000 limit.

The reason this is the interesting part of the project is that it is **falsifiable**. Either the replacement lot splits share by share or it does not. Either the holding period tacks once per matched share or it compounds. There is a right answer, it is written down in the Code and the Regulations, and there are sixty-three tests establishing which answer this engine gives.

On top of that law, HarvestIQ reports three figures on every recommendation: what it saves you now, what it costs you later once your basis has been lowered, and the net.

### What the engineering actually is

The architecture is not incidental to the tax work. It is what makes the tax work checkable, and almost every structural decision traces to one of two constraints: the tax logic has to be testable without a browser, a worker, a database or a network, and financially consequential records must never be left half written.

**A pure domain core behind a one-way import rule.** The imports run in one direction only, from the web layer to the background worker to the tax engine to the schema, and never back. The engine knows about a database session and nothing else. No cache, no queue, no web framework. That is precisely what lets sixty-three tests exercise the netting waterfall, wash-sale enforcement, lot splitting and the whole sequenced plan with no browser, no worker and no network running. The one-way rule is the entire discipline: the moment the engine imports the cache, every test needs a cache.

**An atomic ledger.** Recording a sale writes a transaction, reduces one or more lots, and writes one realized gain per lot consumed. The original code saved to the database at three points inside that sequence, so a failure between any two left records that were internally inconsistent and could not be repaired by retrying. Nothing raises an error in that state. It just produces confident, wrong tax figures forever. The intermediate saves became flushes, which hand back database identifiers without creating a durable boundary, and a single save happens at the end. That change is also what later made transaction-time wash-sale enforcement possible, because both directions modify records written moments earlier in the same operation and are only safe inside it because the operation is atomic.

**Exact decimals from the database to the browser.** Cost basis feeds every calculation in the system, and a figure that round-trips through a floating-point representation comes back slightly wrong, then gets written back. Exact decimal types in the database and the application, money and quantities serialized as text across the interface so no client can round them by accident, tax rates bounded between zero and one at the boundary, and rates crossing the worker queue as text because the queue cannot serialize decimals.

**Migration discipline.** Eleven migrations, and every tax change ships as one. Three of them exist because the law demanded a schema change rather than a logic change: the separate holding-period field, the wash-sale basis and consumed-capacity columns, and the watch-only external accounts. That is the tell that the legal work was real, since a heuristic never needs a new column.

**Three processes that share no memory.** A web process serving the interface and the browser socket, a background worker running the tax recompute and the language model, and a streamer holding the market data connection. They share nothing in memory and communicate only through the database for durable state and the cache for pub/sub, with one-terminal startup for development.

**Two cadences for two costs.** Prices flush to the database once a second, and the tax analysis recomputes every thirty seconds. Ticks arrive faster than a commit completes, so committing per tick means the database never catches up. The streamer therefore holds prices in memory and writes them back on a fixed interval, which is a write-back cache with the staleness bounded deliberately at one second.

**One subscription and one computation for every connected client, not one per browser.** The listener does nothing but raise a flag, so a slow recompute can never back up the subscription. Because it is an event rather than a queue, coalescing is free: many publishes inside one window raise the same flag once. A per-client send timeout stops a single stalled socket from holding up the broadcast to everyone else. And the event is created inside the application lifespan rather than at import, because it binds to the first event loop that awaits it, so a module-level one breaks the moment the app runs on a second loop, which is exactly what the test suite does.

**Load-once query discipline.** The opportunity builder previously ran two queries per lot. Securities are now loaded once into a lookup table and the wash-window lots gathered in a single pass, and the capacity calculation accepts already-loaded lots and securities from callers that hold them, so the live summary path computes capacity at no additional query cost at all.

**Interface conventions that do not make the caller guess.** Writes return the created resource. Work that gets queued returns an accepted status with a job identifier and a location header pointing at it. Asking for an analysis that has never been computed returns a service-unavailable status rather than a success response carrying an empty body, which every client would otherwise have to inspect to find out whether it actually got an answer.

None of this is required by the tax code. It is what makes an implementation of the tax code something you can trust, test, and correct when it turns out to be wrong.

**Who it's for.** A self-directed investor with a taxable brokerage account, crypto across several wallets, or both, who would rather harvest deliberately than hand the decision to a robo-advisor, and who wants to see the reasoning before acting on it. The system is single-user with no authentication yet, which is the main thing standing between it and production use.

**What it will not do.** Issue a recommendation in the cases where harvesting produces no benefit, or produces a net cost. Those get reported as such instead.

---

## How HarvestIQ works

Read this section first. Everything after it is a detail of one of these eight stages, and the capabilities will feel arbitrary without it.

### Stage 1. The ledger

Everything starts with an accurate record of what you own, at the level the tax authority cares about.

You record transactions. Every purchase opens a **tax lot**, which is a distinct record of that specific buy, carrying its own cost basis, its own acquisition date, and the account it lives in. Every sale consumes lots according to a method you choose, and writes one realized gain or loss record per lot consumed, classified as short-term or long-term.

This matters because a "position" is a convenience the screen shows you. If you bought the same stock three times, you own three lots with three different cost bases and three different clocks, and which one you sell changes your tax bill. Everything in the rest of the pipeline reads from these records, so if the ledger is wrong, nothing downstream can be right.

*Covered by capabilities 26 to 31, and 35 to 37.*

### Stage 2. Valuation

Live prices arrive continuously from a market data feed and mark every lot to market. Any lot now worth less than its cost basis is an unrealized loss, and therefore a harvest candidate.

At this point the engine has a list of candidates and no idea what any of them are worth.

### Stage 3. Eligibility

Before pricing anything, the engine checks whether each candidate's loss is one you are actually allowed to claim.

The **wash-sale rule** disallows a loss if you bought the same or a substantially identical security within thirty days either side of the sale. The rule applies to you as a taxpayer across every account you hold, while each broker only sees the account it custodies. So HarvestIQ checks purchases across all of your registered accounts, including watch-only accounts you never trade in, such as a spouse's account or an IRA.

Candidates that fail this check are set aside with the reason, rather than silently dropped.

*Covered by capabilities 15 to 25.*

### Stage 4. Pricing

For each eligible candidate, the engine works out what selling it would actually save you in tax this year.

It does this by computing your complete tax outcome with the harvest and without it, then taking the difference. That calculation runs the statutory netting sequence, accounts for the gains you have already realized and their classification, subtracts losses carried forward from prior years, and applies the $3,000 ordinary-income limitation. It returns two separate figures: the cash saving this year, and the carryforward the harvest banks for later.

It then adds a third figure, which is the future tax the harvest creates by lowering your cost basis.

*Covered by capabilities 1 to 7.*

### Stage 5. Sequencing and sizing

Now the engine has a priced list. It does not simply sort it, because the figures do not survive each other.

Harvesting the first loss consumes the gains that made every other loss valuable. So the plan is built by simulation: price all remaining candidates against your current position, take the best one, apply its loss, then re-price everything that is left and repeat. Each step is also sized rather than taken whole, because unrealized loss is a finite resource and selling more of it than the benefit requires is waste.

The output is an ordered plan. Each step names a specific lot, a specific quantity, and the three figures.

*Covered by capabilities 8 to 14.*

### Stage 6. Warnings

Two conditions quietly destroy the value of an otherwise sound harvest, and neither is visible from the position itself. Automatic dividend reinvestment counts as a purchase and can trigger a wash sale on a position you thought you had fully exited. And a short-term loss approaching its one-year anniversary is about to become a long-term loss, which is usually worth less.

Both are checked before the sale, because neither can be fixed afterwards.

*Covered by capabilities 32 and 33.*

### Stage 7. Capacity

Separately from what to sell this year, the engine reports how much harvestable loss exists in the account at all, and how far each holding would have to fall before it offers anything.

These are genuinely different questions with different answers, and conflating them hides the fact that harvesting permanently drains a pool that only new contributions refill.

*Covered by capability 14.*

### Stage 8. Execution and explanation

Because each plan step names a specific lot, the dashboard executes it directly rather than leaving you to reproduce it by hand in a broker interface, where lot selection is usually out of your control.

A language model then narrates the finished plan in plain English. It computes nothing. Every figure it describes was already determined by the engine.

*Covered by capabilities 34, 40, and 42.*

---

## The law encoded

The engine is not a set of heuristics about losing positions. It is an implementation of specific statutory and regulatory rules, and this is the list of them. Each row names the rule, the authority it comes from, and the capability that implements it.

| Rule implemented | Authority | Capability |
|---|---|---|
| Net capital loss deductible against ordinary income is capped at $3,000 a year | IRC §1211(b) | 1, 2 |
| Excess loss carries forward and retains its short-term or long-term character | IRC §1212(b) | 3, 5, 6 |
| The $3,000 ordinary-income offset draws from short-term losses first | IRC §1212(b) | 3 |
| Long-term means held for more than one year, measured on the calendar | IRC §1222; Treas. Reg. §1.1223-1 | 31, 33 |
| A loss is disallowed where substantially identical securities are acquired within 30 days before or after the sale | IRC §1091(a) | 15, 16, 17, 32 |
| The rule applies to the taxpayer, across every account held, not to the account | IRC §1091(a) | 22, 23 |
| The disallowed loss is added to the cost basis of the replacement shares | IRC §1091(d) | 16, 18, 36 |
| The holding period of the shares sold is added to that of the replacement shares | IRC §1223(3) | 19, 20 |
| Shares are matched one for one in order of acquisition, so a partial wash splits the replacement lot | Treas. Reg. §1.1091-1 | 18 |
| A replacement share can absorb only one disallowed loss | Treas. Reg. §1.1091-1 | 21 |
| No basis adjustment arises where the replacement is bought in an IRA, so the loss is destroyed rather than deferred | Rev. Rul. 2008-5 | 24 |
| Virtual currency is property rather than a security, so the wash-sale rule does not reach it | Notice 2014-21 | 25 |
| Cost basis for digital assets must be tracked per wallet or account, for tax years beginning after 31 December 2024 | Rev. Proc. 2024-28; Treas. Reg. §1.1012-1(j) | 26, 28 |
| Basis is cost | IRC §1012 | 30 |
| A seller may specifically identify which shares are sold | Treas. Reg. §1.1012-1(c) | 29 |

**Where a capability carries no authority line, that is deliberate and it means something.** Twenty-five of the forty-two implement a rule that comes from somewhere, and they cite it. The other seventeen are engineering or product decisions that no statute requires: sequencing the plan rather than ranking it, sizing each sale to the useful-loss budget, the carryforward dial, reporting what the plan declined to recommend, measuring remaining capacity, writing the ledger atomically, keeping the language model away from the figures. The law does not ask for any of those. They are choices, and several of them are the ones I would most want to defend in an interview.

Keeping the two apart matters, because a reader can then tell which parts of the engine are correct-or-incorrect against an external authority and which parts are my judgement about how a harvesting tool ought to behave.

**What is deliberately not implemented is listed with the same specificity** in capability 41, and includes bracket determination, the net investment income surtax, the §1(h) rate groups, state treatment, the §1211(b) requirement that losses be used to the maximum extent in the current year, the §852(b)(4)(A) six-month rule, the §246(c) 61-day qualified-dividend rule, and §301(c)(2) return of capital. The distinction that matters is that the harvesting mechanics are implemented against the authorities above, while the rate applied at the end is a figure the user supplies.

---

## The tax mechanics behind all of it

Seven concepts the rest of this document depends on. Skip if they are familiar.

**Cost basis.** What you paid to acquire something, including fees. Buy 1 BTC at $30,000 with a $50 fee and your basis is $30,050. Sell at $25,000 and you have a $5,050 loss. Basis is the number every tax calculation subtracts from, which is why most of this engine is really a basis-tracking engine.

**Tax lot.** Each individual purchase is its own lot with its own basis and acquisition date. Buy 10 shares in January and 10 more in March and you own two lots, not a 20-share position. When you sell 10, which lot they come from determines the result.

**Short-term and long-term.** Hold something more than one year and any gain gets preferential rates of 0, 15, or 20 percent. A year or less and it is taxed as ordinary income, up to 37 percent. The same split applies to losses, which matters more than people expect.

**The netting sequence.** Losses cancel gains, but not freely. Short-term nets against short-term first, long-term against long-term, and only then do the two sides combine. So a loss must cancel gains of its own kind before anything else. Concretely: with a $1,000 long-term gain taxed at 15 percent and a $1,000 short-term gain taxed at 35 percent, a $1,000 long-term loss saves you $150, while an identical short-term loss saves you $350.

**The $3,000 limitation and carryforwards.** If losses exceed gains, only $3,000 of the excess comes off your ordinary income each year. The rest becomes a carryforward that rolls into future years and keeps its short-term or long-term character. Carryforwards offset future capital gains without limit; the $3,000 ceiling applies only to the ordinary-income deduction. This is the most misunderstood rule in the domain, and people routinely report correct software as broken because of it.

**The wash-sale rule.** Sell at a loss and buy the same security within thirty days either side, and the loss is disallowed. It is not destroyed: it gets added to the cost basis of the replacement shares, and those shares inherit the holding period of the ones you sold. You recover the benefit when you eventually sell them. The rule applies to you across all accounts.

**Deferral versus reduction.** Harvesting lowers your replacement position's basis by exactly the loss you took, so the eventual sale owes more. If your future rate matches today's, the lifetime benefit is close to zero and you have taken an interest-free loan rather than found free money. If your future rate is higher, harvesting loses you money. And if the gain you offset would have been taxed at zero percent anyway, you spent a real loss to shelter something that was already free.

**The case the whole product is built around.** You own 100 shares bought at $200, so a $20,000 basis, now worth $15,000. You have separately realized $8,000 of short-term gain this year. Selling realizes a $5,000 loss, which cancels that gain and saves you **$1,850** at a 37 percent rate. But your replacement position carries a $15,000 basis instead of $20,000, so you will owe roughly **$750** more when you eventually sell at a 15 percent long-term rate. The net lifetime benefit is about **$1,100**, not $1,850.

Most tools show you $1,850. HarvestIQ shows all three numbers on every recommendation.

---

# THE TAX ENGINE — CAPABILITIES

*Forty-two capabilities, grouped by the stage of the workflow they belong to. Each one opens with what it is actually doing, then the problem it solves and the mechanism behind it.*

---

## STAGE 4 — PRICING: what a loss is worth

### 1. HarvestIQ implements the statutory netting waterfall

> **It nets short-term against short-term and long-term against long-term, cross-nets whatever survives, applies the $3,000 ordinary-income limitation, and preserves the classification of the remaining loss.**

**What this step is doing.** Every lot currently worth less than you paid for it is a harvest candidate. For each one, the engine answers a single question: if you sold this lot today, how much less tax would you owe this year? That figure is the foundation for everything downstream. It ranks the candidates, decides where the plan stops, and becomes the savings number on the dashboard. If it is wrong, every recommendation built on it is wrong too.

**The problem.** Almost every tool answers that question the same way. Take the loss, multiply it by the tax rate for that lot's holding period, call the result the saving. It looks reasonable and it is wrong in three separate ways at once.

First, it never looks at the rest of your year. A loss is only worth something if it has a gain to cancel, and a multiplication has no idea whether you have any gains at all.

Second, it treats all gains as interchangeable, which the IRS does not. A loss has to cancel gains of its own kind first. So a $1,000 short-term loss landing on short-term gains taxed at 37 percent is worth $370, while the same $1,000 loss with only long-term gains available is worth $150. Same loss, same size, less than half the value. A flat rate cannot express that difference.

Third, the benefit runs out. Once your gains are fully covered, only $3,000 of whatever remains can come off your ordinary income this year. Every dollar past that point is worth nothing until some future year, but the multiplication keeps returning a number regardless.

Together those produce a wrong recommendation, not a rounding error. Consider someone already sitting at a net loss for the year. Their gains are gone, the $3,000 is spent, and one more harvest is genuinely worth zero to them. A flat-rate engine tells them it is worth thousands. They sell the position, permanently burn a loss they cannot get back, and receive nothing for it.

**How it works.** The engine runs the netting sequence in stages, the way the tax authority runs it. Short-term losses net against short-term gains, and long-term against long-term. Whatever survives on one side then cross-nets against the other. Any loss still standing keeps its classification rather than becoming generic, which matters because a carryforward returns the following year as the same character of loss it was, and that character decides what it is allowed to offset when it arrives.

*Authority: IRC §1211(b).*

### 2. HarvestIQ treats the $3,000 ordinary-income deduction as a refund

> **The engine allows a computed tax liability to come out negative**, because a net capital loss deducted against ordinary income is money back rather than an absence of tax.

**What this step is doing.** Turning a netted tax position into an actual dollar figure for the year, including the case where you have no capital gains at all and the only thing harvesting does for you is reduce the tax on your salary.

**The problem.** The obvious implementation floors the tax liability at zero, on the reasoning that you cannot owe less than nothing. But the $3,000 deduction does not reduce tax on your capital gains, it reduces tax on your other income. Flooring the figure at zero quietly deletes it.

That is a serious loss of function rather than an edge case, because the small harvester is exactly the person who has no gains to offset and is harvesting purely for that deduction. For them, the floored version reports that harvesting is worth nothing at all.

**How it works.** The current-year outcome is calculated as tax on gains, less the value of the ordinary-income offset, and the result is returned whatever its sign. Anything reading that figure treats a negative number as a refund rather than as an error to be corrected.

*Authority: IRC §1211(b).*

### 3. HarvestIQ applies short-term-first offset ordering

> **It follows the statutory ordering rule**, so the carryforward that survives into next year skews long-term, which is what determines next year's offsetting capacity.

**What this step is doing.** Deciding which of your losses gets consumed by the $3,000 ordinary-income deduction, when you have both short-term and long-term losses available to fill it.

**The problem.** This looks like an arbitrary implementation detail and it is not. The statute consumes short-term losses first, and the order determines what is left over.

Get it backwards and the wrong character of loss survives into the carryforward. That misprices every subsequent year, because a long-term carryforward cannot take first claim on next year's short-term gains, while a short-term one can. The error is invisible this year and compounds into the next.

**How it works.** The offset is drawn out of the short-term loss pool before the long-term pool, and the two surviving carryforward figures are reported separately so that each keeps its own identity.

*Authority: IRC §1212(b).*

### 4. HarvestIQ uses before-and-after differential valuation

> **It computes your full tax outcome with the harvest and without it, and returns the difference**, as two separate figures: the current-year saving and the carryforward generated.

**What this step is doing.** Producing the actual saving figure for a single candidate lot, which is the number capability 1 set out to find.

**The problem.** Even with the netting sequence implemented correctly, you cannot get to the answer by multiplying, because what you are measuring is a step function rather than a straight line.

The first slice of loss cancels gains at one rate. The next slice falls inside the $3,000 allowance and is worth a different rate. Everything beyond that is worth nothing this year. Where you happen to be sitting on that staircase is the entire question, and no single multiplication can describe a shape with steps in it.

**How it works.** The engine calculates your complete tax outcome before the harvest, applies the loss to the correct classification, calculates the outcome again, and subtracts. Nothing anywhere in the system multiplies a loss by a rate.

There is a second reason this structure was worth the extra work. The naive part of the model, which is the flat user-supplied rate, now lives inside one function. Replacing it with real bracket calculation and the investment surtax means substituting that function, after which every figure in the product improves at once. The biggest known weakness became a contained upgrade rather than a rewrite.

### 5. HarvestIQ prices against the effective net position

> **Losses carried forward from prior years are subtracted from this year's realized gains before any harvest is valued.**

**What this step is doing.** Establishing the starting position that every candidate gets priced against.

**The problem.** Someone carrying $22,000 of losses forward has already sheltered this year's gains before they harvest anything new. If the engine prices against realized gains alone, it reports each new loss at full value when the true marginal value is zero.

There is also a data problem underneath. Carryforwards are the one tax input the engine cannot work out for itself, because it only knows about transactions recorded in it and the carryforward comes off last year's return.

**How it works.** A settings record holds your carryforwards as positive loss amounts, split by classification. Positive because that is how they appear on a return, and split because they re-enter the year carrying the character they had when they were created. Every valuation starts from realized gains minus carryforward rather than from realized gains alone.

*Authority: IRC §1212(b), under which a carryforward re-enters the following year retaining its character.*

### 6. HarvestIQ separates current-year savings from carryforward generated

> **The two figures are kept apart everywhere** and are never added together into a single number.

**What this step is doing.** Reporting the output of the pricing stage honestly, given that a harvest usually produces two different kinds of value at once.

**The problem.** Cash saved this year and loss banked for later are not the same thing and should not share a field. The saving reduces tax you owe now. The carryforward is a claim on future years, worth something only if gains actually materialize, at whatever rate applies then, and only if you use it before it lapses. Unused carryforwards expire at death and cannot be inherited.

Adding them together and calling the total "savings" presents deferred value as cash, which is precisely the kind of overstatement the enforcement actions in this sector were about.

The confusion also runs the other way among users. Many believe the $3,000 limitation caps all loss usage, when it caps only the ordinary-income deduction. Carryforwards offset capital gains without limit, and correct software gets reported as broken on this basis regularly.

**How it works.** The two figures never share a field, on opportunities, on plan steps, or on totals. Lots whose entire value is deferral do not appear among the recommended steps at all. They go into a separate collection, so a deliberate decision to defer can never be misread as a savings recommendation.

*Authority: IRC §1211(b) for the ordinary-income cap; IRC §1212(b) for the carryforward.*

### 7. HarvestIQ implements deferral accounting, showing saves now, costs later, and net

> **Three figures on every recommendation**, because harvesting is mostly deferral rather than reduction.

**What this step is doing.** Adding the third and least comfortable number to the output: what this harvest will cost you later.

**The problem.** Harvesting lowers your replacement position's cost basis by exactly the loss you realized, so the eventual sale owes more tax. If your future rate matches today's, the lifetime benefit is close to zero. If it is higher, harvesting actively loses you money. And if the gain you offset would have been taxed at zero percent, you spent a real loss to shelter something that was already free.

This is not a pedantic distinction. Published estimates of harvesting's lifetime value range from roughly 0.15 percent a year to 1.1 percent, which is nearly an order of magnitude, and the spread is explained almost entirely by whether the study debits the future tax it creates.

**How it works.** Three figures are produced for every harvested loss:

```
what it saves now   = the current-year reduction in tax owed
what it costs later = the loss × your expected future rate
the net             = saves now + (carryforward × future rate) − costs later
```

Your expected future rate is a setting. Leaving it unset defaults to today's long-term rate, which is the neutral interest-free-loan case, presented as a visible default rather than an assumption made quietly on your behalf.

The approximations are written down rather than left implicit. The eventual sale is assumed long-term. Carryforward is valued at the same future rate regardless of its classification. No discounting is applied for time value. And no credit is taken for the two ways deferred tax can disappear entirely, which are a basis step-up on inheritance and charitable donation.

**The worked case.** 100 shares bought at $200, now worth $150, against $8,000 of short-term gain already realized. The harvest realizes $5,000 of loss, cancels that gain, and saves **$1,850** at 37 percent. The replacement carries a $15,000 basis instead of $20,000, so eventual disposal owes roughly **$750** more at 15 percent. Net **about $1,100**, not $1,850.

*Authority: the future cost follows from IRC §1012, since the replacement position's basis is its own cost. The two ways the deferred tax can disappear entirely are the basis step-up at death under IRC §1014 and charitable donation under IRC §170. Whether to discount for either is a modelling choice rather than a rule, and this engine does neither.*

---

## STAGE 5 — SEQUENCING AND SIZING: which losses to take

### 8. HarvestIQ uses greedy sequential re-pricing

> **It re-prices every remaining candidate after each step**, because realizing one loss changes what all the others are worth.

**What this step is doing.** Turning a priced list of candidates into an ordered plan you can actually execute.

**The problem.** A ranked list prices every lot against the same starting position. Those figures are each individually correct and mutually exclusive, so they do not add up. Putting a total underneath such a list sums several alternative futures, only one of which can happen.

The ordering goes stale too, not just the amounts. Once your short-term gains are used up, a short-term loss that ranked first can correctly fall behind a long-term one, because the thing that made it valuable was first claim on short-term gains and those are now gone. A static ranking cannot represent that shift.

**How it works.** The plan simulates actually doing it. Price every remaining candidate against the current position, take the highest scorer, apply its loss to that position, then re-price everything left and repeat. Each step's saving is therefore its true value given everything harvested before it, and the cumulative total is what executing the whole plan actually yields.

### 9. HarvestIQ sources its headline figure from the sequenced plan

> **The dashboard's savings number is the plan total** whenever a plan exists, because per-lot figures do not survive each other.

**What this step is doing.** Choosing which of two available numbers the user sees as the headline.

**The problem.** Two totals exist and they disagree. Summing the per-lot valuations overstates, for the reason in capability 8. Whichever number occupies the headline is what the user believes the strategy is worth, so this is a disclosure decision as much as a display one.

**How it works.** The portfolio summary takes its savings total from the cached plan when there is one, and falls back to summing per-lot figures only when there is not. The fallback carries a note in the code stating plainly that it overstates and why, so nobody later mistakes it for the preferred path.

### 10. HarvestIQ implements partial-lot sizing against a useful-loss budget

> **It sells part of a lot when part is all that helps**, sized continuously by a dial expressing what you think banked carryforward is worth.

**What this step is doing.** Deciding how much of a chosen lot to actually sell, rather than assuming the answer is all of it.

**The problem.** This one was found by running the engine against constructed inputs during the audit rather than by reading the code.

Take a user with $1,000 of short-term gains at 37 percent. They hold one position carrying $4,000 of unrealized loss and another carrying $40,000. Both produce identical current-year savings of $1,480, because both fully cover the gain and both then hit the $3,000 limitation. The tie-break picked the $40,000 position, consuming ten times the resource for exactly the same benefit.

That only reads as a defect once you see that unrealized loss is scarce. Harvesting means repurchasing at the lower price, which resets your basis downward, so the loss is consumed and does not come back. Only new money added to the account creates fresh high-basis lots capable of falling below cost later. This is the mechanism behind tax alpha decay, where a harvested portfolio slowly ossifies until it cannot produce losses at all.

**How it works.**

```
disposal size = the useful-loss budget + (the rest of the lot × your carryforward dial)
```

The useful-loss budget is the loss that still produces a current-year benefit, meaning enough to zero out any net gain plus whatever room is left under the $3,000 allowance. The dial runs from 0 to 1 and moves both the ranking score and the sell size continuously.

| Dial | What it means | What the plan does |
|---|---|---|
| 0 | No gains coming | Sells exactly to the budget. A lot with $40,000 of loss against a $4,000 budget becomes a $4,000 partial sale |
| 0.3 | Some gains likely | The budget plus 30 percent of the remainder |
| 1 | A large gain is coming, such as a property sale or vesting cliff | Values carryforward at face and banks whole lots |

Each step carries the exact quantity and a partial flag, so a sized disposal executes as sized rather than rounding up.

### 11. HarvestIQ picks the best lot on an explicit score, not a proxy

> **This is the selection rule in full: eligibility, then size, then marginal value against the running position, then one score, then documented tie-breaks.**

**What this step is doing.** Answering the central question of the whole product, which is which lot to sell next.

**The problem.** Every intuitive proxy for that question is wrong.

Ranking by the size of the unrealized loss picks the biggest loser, which is usually the worst choice, because it spends the most of a finite resource for a benefit that may be capped far below it. Ranking by percentage decline picks the most damaged holding, which has no relationship to what its loss is worth in tax. Ranking by position-level profit and loss ignores that a position is several lots with different bases and different clocks, one of which may be underwater while the position overall is up. And ranking by current-year saving alone cannot separate two lots that save the same amount but bank very different quantities of carryforward, or that reach the same saving at very different cost in loss consumed.

Leave the rule unstated and it becomes whatever the sort happens to do. That is not hypothetical. It is exactly how the defect in capability 10 arose.

**How it works.** Each round runs the same five steps.

**One, eligibility.** Lots whose loss would be disallowed under the wash-sale rule are removed before anything is scored, and recorded separately with the reason. A lot that cannot legally deliver its loss is not a candidate at any price.

**Two, size.** Each remaining candidate is sized to the loss that still produces benefit. Scoring then happens on the sized loss rather than the full one, so the score describes the transaction actually being proposed.

**Three, marginal value.** Each sized candidate is priced against the current running position, meaning this year's realized gains by classification, less prior-year carryforwards, less everything the plan has already harvested in earlier steps. This is the step that makes the selection intelligent rather than static, because it is what knows a short-term loss is worth more while short-term gains remain and worth no more than a long-term one once they are gone.

**Four, the score.** One figure per candidate:

```
score = the current-year tax saving
      + the carryforward it generates × your expected future rate × your dial
```

The first term is real money this year. The second is banked value, deliberately discounted by what you say it is worth to you, which is why a taxpayer expecting no future gains and one expecting a large liquidity event get different plans from identical portfolios.

**Five, tie-breaks, in order.** At equal score, prefer the candidate that consumes less unrealized loss, because that resource does not regenerate. At equal loss consumed, prefer a whole-lot disposal, because it is simpler to execute and to record.

The highest scorer is taken, its loss is applied to the running position, and the round repeats. The plan stops when the best remaining score falls to your threshold, and it says which of the two possible reasons it stopped for.

**What the score deliberately leaves out.** It contains no view on the security, no conviction about recovery, and no preference between symbols or sectors. HarvestIQ decides which lot is most valuable to sell for tax purposes. Whether you want to keep that market exposure is a portfolio decision it does not make for you, which is also why replacement suggestions remain a labelled placeholder rather than a recommendation.

**A related audit finding, closed without a code change.** The audit recommended ranking by saving per dollar of loss consumed. Checked against the implementation and rejected, because the tie-break already prefers consuming less loss at equal score, and with partial sizing in place the plan never takes more than the budget requires anyway. Recording the rejection matters, because an item closed by verification leaves no commit to point at, and re-deriving the conclusion later would cost the same effort as the original analysis.

### 12. HarvestIQ makes the carryforward dial continuous, not binary

> **A shipped defect: any nonzero dial setting produced identical whole-lot disposals.** Only the ranking varied continuously.

**What this step is doing.** Making the sizing rule in capability 10 actually respond to the setting that is supposed to control it.

**The problem.** After the sizing feature shipped, the plan still contained an older branch that asked only whether the dial was above zero before deciding the sell size. So every value from 0.01 to 1.0 behaved identically.

Concretely: a user with $4,000 of useful loss budget holding a position with $40,000 of loss gets a $4,000 sale at dial 0, as documented. At dial 0.05, meaning mildly interested in banking losses against a gain that may not arrive, they get the entire $40,000, exactly as if they had set the dial to 1. Nothing in the interface distinguishes those two outcomes, so a cautious preference produced the most aggressive behaviour available.

It survived a completely green test suite because the existing dial tests covered 0 and 1 only. Both endpoints, both genuinely correct.

**How it works.** The branch was replaced with a formula that is continuous by construction, so dial 0.05 now sells $5,800 and the endpoints land exactly where the documentation says they do.

The detail that matters is which quantity the formula reads. It uses the dial itself, not the dial multiplied by your expected future rate. Those two are easy to confuse because both are fractions sitting in the same expression, but only one is dimensionless. The multiplied version is a tax rate, typically around 0.15 to 0.20, so using it would have capped even the fullest setting at roughly a fifth of the remainder.

The regression test samples the middle of the range and asserts the sell size moves monotonically across it, because testing only the endpoints is precisely what let this through the first time.

### 13. HarvestIQ reports the plan's negative space

> **Every losing lot appears in the output in exactly one of four states**: recommended, deferred by choice, blocked by rule, or below your threshold. There is no silently omitted category.

**What this step is doing.** Reporting what the plan decided not to do, alongside what it decided to do.

**The problem.** A plan that shows only its recommendations cannot be checked by the person using it. A lot sitting at a large unrealized loss that does not appear among the steps looks identical to a lot the engine simply failed to consider.

And there are three genuinely different reasons a losing lot might be absent, each with different implications. It might be ineligible, because replacement shares inside the wash-sale window would cause the loss to be disallowed, which is a temporary condition with a known expiry that you might be able to resolve by selling the replacement shares instead. It might be eligible but worthless this year, because the useful-loss budget is spent and further loss produces only carryforward, which is a deliberate deferral rather than an oversight. Or it might fall below a threshold you set yourself.

**How it works.** The plan returns its negative space as structured output rather than throwing it away.

The blocked list names every lot withheld for ineligibility, along with the specific replacement shares responsible, their acquisition dates, the accounts holding them, and whether any of those accounts would cause the loss to be forfeited permanently rather than merely deferred.

The deferred list holds every lot that is eligible but produces no further current-year benefit. These are priced at the position remaining after the plan has executed rather than at the starting position, so the figures describe the decision actually being deferred. They also carry the seasoning deadlines, which matter more here than anywhere else in the output, because these are exactly the lots being held into the following tax year.

A stated reason for stopping distinguishes a plan that harvested everything available from one that stopped because harvesting more would achieve nothing. And a threshold you supply sets the floor below which a step is not worth executing, so that judgement is an explicit input rather than a constant buried in the engine.

### 14. HarvestIQ measures harvest capacity and drawdown-to-harvest

> **It reports the remaining pool of unrealized loss separately from this year's recommendation**, plus how far each holding must fall before it offers anything at all.

**What this step is doing.** Answering a question none of the other figures answer, which is how much harvestable loss is left in the account at all.

**The problem.** Everything else in the engine answers what to sell today. Nothing answered how much there is to sell ever, and the two diverge badly.

The recommended figure is capped by the useful-loss budget and the dial. So an account holding $80,000 of unrealized loss can report $4,000 of recommended harvest and look nearly exhausted when it is not. Worse, it can report $4,000 in each of ten consecutive years while the underlying pool quietly drains to $6,000. The headline number is identical at the start and near the end, so the user gets no signal at all that the account is running out.

**How it works.** The capacity calculation deliberately ignores both the annual budget and the dial, and reports the pool itself: the total harvestable loss, how much is available right now, and how much is currently blocked under the wash-sale rule. Blocked losses count toward the total but not toward the available figure, because they are unavailable today rather than gone, and omitting them would misread a temporary constraint as permanent depletion. Capacity is also reported as a share of account value.

For each holding it reports the fall required before that holding offers anything, taken from its highest-cost lot, because that lot goes underwater first and therefore sets the threshold for the whole holding. Zero means something is harvestable now. Thirty percent means the holding is ossified and offers nothing short of a serious decline.

**Drawdown-to-harvest is not a volatility measure**, and the distinction matters. It is a static ratio between the current price and the highest cost basis held, so it tells you how far the price must move and not how likely it is to move that far. Two holdings can report an identical 30 percent figure while one is a utility that has never moved 30 percent in a decade and the other routinely does so in a quarter. Reading it as risk-adjusted would invert the conclusion in exactly the cases where it matters most.

The engine holds no volatility, standard deviation, correlation, or beta anywhere. Ninety days of daily price history is retrieved, but only to draw charts and to supply a fallback quote when the live feed has not priced a symbol. It never reaches a tax or selection calculation. Combining the drawdown figure with realized volatility to produce a probability that a holding becomes harvestable within some horizon is a real and cheap extension, and it is not built.

**Deliberately excluded: the contribution planner.** The obvious next feature is to size a recommended cash contribution and route it around open repurchase windows. It is a genuine differentiator that no competitor implements, and HarvestIQ holds the cross-account data to do it properly. It is excluded because it is the point where an asset manager's incentives and a user's diverge. Contributions do not create value, they restore harvest capacity. The diagnostic half is the honest half and stands on its own. If it is ever built, the framing has to be conditional on the money already existing, never a recommendation to contribute in order to harvest.

---

## STAGE 3 — ELIGIBILITY: the wash-sale rule

### 15. HarvestIQ detects wash sales by replacement shares retained

> **The test is whether you would still hold replacement shares after the sale**, so completely liquidating a recently bought position is correctly not flagged.

**What this step is doing.** Screening each harvest candidate before it is priced, to establish whether the loss is one you are allowed to claim at all.

**The problem.** The original check flagged exposure wherever any purchase of that security existed inside the window, and that excludes precisely the wrong candidates.

Suppose you bought a position three weeks ago, it dropped sharply, and you now sell the whole thing. No wash sale has occurred, because nothing was replaced. You hold nothing afterwards. A sharp recent drawdown is one of the strongest harvesting candidates there is, and the original test suppressed every single one of them.

**How it works.** A wash sale needs replacement shares, meaning shares bought inside the window that you would still be holding after the sale. Three consequences follow, and each one is a decision.

Detection reads open tax lots rather than transactions, because shares you have already sold cannot serve as replacements for anything. The lot being evaluated excludes itself from its own replacement pool, since selling it removes its own shares from consideration, and that is what makes complete liquidation of a fresh purchase correctly unflagged. And shares that have already absorbed a disallowed loss are netted out, because a share can only absorb one.

Every candidate also carries a safe-to-repurchase date, because selling opens the forward half of the window and buying back on or before that date disallows the loss you just harvested.

*Authority: IRC §1091(a), which disallows the loss where substantially identical stock or securities are acquired within 30 days before or after the sale.*

### 16. HarvestIQ enforces wash sales against the ledger, not just the recommendation

> **It applies the rule to your records at the moment of the transaction**, disallowing the loss, moving it into the replacement shares' cost basis, and carrying the holding period across.

**What this step is doing.** Making the wash-sale rule affect your actual books, rather than only affecting which candidates get suggested.

**The problem.** The original implementation evaluated the rule in exactly one place, while assembling recommendations. If a candidate looked exposed, it was dropped from the list. That was the entire behaviour of the rule.

Which means the rule had no effect whatsoever on the accounting. If the sale happened anyway, and there are three ordinary ways it might, the ledger recorded the full loss as allowable and never revised it. You might have acted outside the recommendation. You might have harvested a lot the engine never flagged. Or the replacement purchase might have happened afterwards, where no amount of checking at recommendation time could have known about it.

From there the error spreads. Your realized totals for the year are wrong, the netting position is wrong, every subsequent harvest valuation is wrong, and the recorded cost basis of the position you are actually holding is wrong.

The two consequences of a wash sale are also separable, and detection-only delivered neither of them. The disallowed amount is supposed to move into the replacement shares' basis, and the holding period of the shares you sold is supposed to move with it. A tool that only warns leaves you holding a position whose recorded basis is understated by exactly the disallowed amount, and that error sits there until you sell.

**How it works.** Detection was kept for its original forward-looking job, which is deciding what to recommend. Enforcement was added alongside it as a separate mechanism that operates on the records themselves, running at transaction time inside the same atomic boundary as the transaction it belongs to.

The two now read the same ledger rather than working independently. The share-level counter that enforcement decrements when it attaches a loss is the same counter detection consults when it asks whether any replacement capacity is left.

*Authority: IRC §1091 and §1223(3).*

### 17. HarvestIQ enforces both halves of the wash-sale window

> **A sale-side check and a purchase-side check**, because the window runs thirty days before the sale and thirty days after it.

**What this step is doing.** Catching the version of the rule that fires after the harvest has already happened.

**The problem.** Checking only at the point of sale covers purchases you already made. It cannot cover the case where you harvest a loss and then buy the security back three weeks later, which retroactively disallows a loss already recorded as allowable. No amount of care at sale time can anticipate a purchase that has not happened yet.

Without the second check the ledger drifts away from what the tax authority will compute, and you find out at the end of the year on a form.

**How it works.** The sale-side check disallows newly realized losses to the extent replacement shares are held. The purchase-side check works backwards from every new purchase to any loss realized inside the preceding window, disallows it retroactively, and rolls the disallowed amount into the new lot's cost basis. A third pair of checks does the same for purchases recorded in watch-only external accounts. All of them are skipped for crypto.

*Authority: IRC §1091(a). The window is symmetric by statute: 30 days before and 30 days after.*

### 18. HarvestIQ implements share-level lot splitting

> **The washed shares are carved into a lot of their own** carrying the adjusted cost and the inherited holding period, while the untouched shares keep their real basis and their real age.

**What this step is doing.** Attaching a disallowed loss to the correct shares when the replacement purchase is larger than the sale that triggered it.

**The problem.** The implementation spread the basis increase and the inherited holding period proportionally across the entire replacement purchase. The regulation requires share-level matching instead. If you sell 40 shares and buy 100 within the window, the whole disallowed loss attaches to 40 of those 100 shares, not thinly across all of them.

Spreading it keeps the aggregate figures right, which is exactly why the bug is invisible, while making every individual share's basis and classification wrong.

The cost shows up on the next harvest. Sell 40 shares at a $400 loss and buy 100 at $50, and the truth is 40 shares at an adjusted $60 carrying the inherited period, plus 60 shares at $50 starting fresh. The smeared version stores 100 shares at $54, all inheriting. Total basis matches at $5,400, so today's reporting looks fine. But at a price of $45, the correct answer is a $300 short-term loss plus a $600 long-term loss, worth about $201, while the smeared version computes $900 entirely long-term, worth about $135.

**How it works.** A partial wash now carves the matched shares into a separate tax lot carrying the adjusted cost and the inherited period, with a reference back to the purchase they came from so the split stays auditable and you can always reconstruct which single broker confirmation produced two rows and why. Full absorption still adjusts the lot in place, which is provably safe because a partially consumed lot has no remaining capacity by construction, so that path can only fire once per lot.

*Authority: Treas. Reg. §1.1091-1.*

### 19. HarvestIQ tacks holding periods once per matched share

> **Carved shares are marked fully spent the moment they are created**, so two sales matched against one purchase cannot age it twice.

**What this step is doing.** Preventing the inherited holding period from being applied to the same shares more than once.

**The problem.** The replacement lot's date was shifted backwards every time the transfer routine ran against it, and the routine runs once per pairing of a disallowed loss with a replacement batch.

So consider buying Apple on 1 June. Two other Apple positions then sell at a loss, one held 200 days and one held 100, and both match against the June shares. The date moves back 200 days, then another 100, and those shares now claim an acquisition date the previous August. They have crossed into long-term treatment nearly a year early. The rule tacks once per matched share, not once per pairing.

The effect is silent. Nothing in the interface shows it, and every subsequent decision on that position is computed from a date that is not real.

**How it works.** The carved lot is created with its replacement capacity already fully consumed. That single assignment does two jobs at once: those shares can never absorb a second disallowed loss, and each sale ages only the shares it actually matched against.

*Authority: IRC §1223(3).*

### 20. HarvestIQ separates the tacked holding date from the acquisition date

> **The purchase date field always holds the real purchase date**, and a second optional field carries the date inherited from a wash sale.

**What this step is doing.** Recording the fiction the wash-sale rule requires without destroying the fact underneath it.

**The problem.** Carrying a holding period across means changing the date used for classification. The obvious way to do that is to overwrite the acquisition date, and it silently breaks two other things, because oldest-first lot ordering and both halves of the wash-sale window all depend on knowing when the shares were genuinely bought.

**How it works.** Two fields. The acquisition date is never modified and keeps recording the actual purchase. The second field is empty when the two agree and diverges only when a wash sale carries a period across.

Every classification in the system reads through a single accessor that prefers the second field and falls back to the first, so no code path can read the wrong one, and records written before the field existed stay valid without a data migration. It is also why seasoning deadlines report a wash-adjusted lot's real anniversary rather than its purchase date.

*Authority: IRC §1223(3).*

### 21. HarvestIQ makes replacement shares single-use

> **A share-level counter, spendable once**, on both internally held lots and purchases observed in outside accounts.

**What this step is doing.** Making sure a share that has already blocked one harvest stops blocking further ones.

**The problem.** The regulation's non-reuse rule says a replacement share absorbs one disallowed loss and is then spent. The engine enforced that for purchases in external accounts but not for its own, where detection summed raw share quantities with no equivalent deduction. Across a sequence of harvests the same shares kept being counted as available, so the engine over-reported exposure.

Over-reporting looks like the safe direction to fail, and it is not. Say you harvest a loss and 100 recently acquired shares absorb it in full. You then look at a second position in the same security, and the opportunity view still counts all 100 as available, reports exposure that cannot actually materialize, and drops the position from the plan. You lose a harvest you were legally entitled to, through a recommendation that simply never appears, which is the least detectable failure mode there is.

**How it works.** A consumed-capacity counter on each lot, and the equivalent on each observed external purchase. Detection nets it out and drops fully spent lots from the pool. Enforcement decrements the same counter when it attaches a loss. Once lot splitting is in place this also resolves structurally, because consumed shares exist as their own lot and fall out of the count by construction rather than needing a subtraction that somebody has to remember to apply.

*Authority: Treas. Reg. §1.1091-1, which matches shares one for one in order of acquisition and does not permit a share to absorb a second disallowed loss.*

### 22. HarvestIQ implements cross-account wash-sale detection

> **Watch-only outside accounts**, such as a spouse's taxable account, an IRA, or a 401(k), contribute their purchases to the window without ever being sold from.

**What this step is doing.** Closing the gap between the unit the law applies to and the unit anyone actually reports on.

**The problem.** The legal unit is you, the taxpayer. The reporting unit is the account. Nothing reconciles them.

Sell at a loss with one broker and buy the same security at another, or in your spouse's account, or in a retirement account, and you have triggered a wash sale that neither broker can see and therefore neither reports. A TurboTax specialist's verbatim answer on whether the software handles this across two brokers: *"No. and neither does professional software used by pros. You'll have to do it manually."*

**How it works.** Outside accounts are registered with a broker name and an account type. HarvestIQ never suggests selling from them. They exist purely so their purchases count against the window, in both detection and enforcement.

Internally held replacement lots take priority over external ones when absorbing a disallowed loss, so the deferred basis stays on your own books where it can be tracked, and only the overflow gets attributed elsewhere.

*Authority: IRC §1091(a), which applies to the taxpayer rather than to the account.*

### 23. HarvestIQ applies retroactive wash checks when an external purchase is recorded

> **Recording a watch-only purchase runs backwards** against every loss realized inside the surrounding window.

**What this step is doing.** Catching the cross-account case where the purchase comes second.

**The problem.** The characteristic version is an automatically investing retirement account buying the same fund a week after you harvested in your taxable account. Nothing about the harvest looked wrong when you made it. You find out months later on a tax form, with nothing you can do about it.

**How it works.** Recording the purchase immediately walks back to any loss realized inside the window and disallows it. Each external purchase carries its own consumed-capacity counter, so one outside buy cannot wash two separate sales.

*Authority: IRC §1091(a). The window runs outward from the purchase as well as from the sale, so a purchase recorded later reaches back to a loss already realized.*

### 24. HarvestIQ models permanent loss forfeiture in tax-advantaged accounts

> **A replacement landing in a retirement account destroys the loss outright**, and HarvestIQ says so rather than reporting an ordinary disallowance.

**What this step is doing.** Distinguishing the version of a wash sale you recover from and the version you never do.

**The problem.** Normally a disallowed loss attaches to the replacement shares' basis and comes back when you sell them. But if the replacement lands in an IRA or a 401(k), there is no basis to adjust, because gains inside those accounts are not taxed that way. The tax authority has ruled explicitly that no basis adjustment arises. The loss is gone permanently.

This is the worst outcome available in tax-loss harvesting. It is exactly what an automatically investing retirement account does to a harvest you made in a taxable account. And no consumer product models it.

**How it works.** The account types that produce this outcome are defined in one place rather than as string comparisons scattered around the code, because whether a loss is destroyed forever is one rule and deserves one definition. Detection marks the candidate as permanent and adds an explicit warning to the stated reason. Enforcement writes a plain-English note on the record distinguishing the two outcomes: a taxable outside account records that the basis adjustment belongs to that account's books so you can reconcile it there, while a retirement account records outright forfeiture.

*Authority: Rev. Rul. 2008-5.*

### 25. HarvestIQ scopes the crypto wash-sale exemption

> **Every wash-sale path is skipped for crypto**, because it is treated as property rather than as a security.

**What this step is doing.** Applying the rule only where it actually applies, and being explicit about where the boundary sits.

**The problem.** Applying the rule to crypto would suppress harvests that are perfectly legal, since you can sell crypto at a loss and buy it back the same day. But the exemption is narrower than it first appears, and the opposite error is worse. Crypto exchange-traded funds and similar products are securities, and they do fall under the rule.

**How it works.** Detection, both enforcement checks, the external-purchase check, and the dividend guard all skip crypto. The boundary is written down rather than glossed over: the schema does not yet separate spot crypto from crypto exchange-traded products, so such a product would currently be treated as exempt when it is not. That sits in the limitations, because the failure mode is a user under-reporting a disallowance.

There is also a consequence for capability 14. The decay analysis assumes the wash-sale rule applies, and for crypto it does not, so a harvested crypto position can be repurchased immediately and picks up none of the wash-driven drift. Crypto ossifies through appreciation alone and arguably needs its own capacity model. The current implementation applies one model to both.

*Authority: Notice 2014-21.*

---

## STAGE 1 — THE LEDGER: what a tax lot is

### 26. HarvestIQ enforces per-wallet cost basis

> **A sale can only draw on lots actually held in that wallet or brokerage account.**

**What this step is doing.** Making sure that when you sell, the engine uses the cost basis of shares you actually hold in the account you are selling from.

**The problem.** Pooling crypto cost basis across all your wallets is prohibited for tax years beginning after 31 December 2024, and unspecified sales now default to oldest-first within the wallet. Treating crypto as one pool produces figures that are simply wrong for the current tax year.

The asymmetry underneath explains why the rule exists. When shares move between brokers, regulation requires the old broker to hand the new one each lot's cost basis and purchase date, so that history is legally required to travel with the shares. When crypto moves between wallets it is just a transaction moving tokens, and nothing carries basis with it. That gap is what let people pool informally, and the new rule closes it as a recordkeeping requirement.

The error is easy to quantify. Buy 10 shares at Broker A with a $150 basis, 10 more at Broker B with a $180 basis, then sell 10 at Broker B for $200. Unscoped oldest-first picks the oldest lot regardless of where it lives, which is Broker A's, and reports a $500 gain. Account-scoped picks Broker B's lot and reports the correct $200. Same sale, same price, $300 of phantom taxable gain created by nothing but which lot's basis got used.

**How it works.** A sale filters open lots by the account it is executing in. Lots you name explicitly are validated one at a time for symbol, account, and open status, each producing its own error, because those are three different mistakes and telling them apart is the difference between a useful message and a useless one.

*Authority: Rev. Proc. 2024-28 and Treas. Reg. §1.1012-1(j).*

### 27. HarvestIQ auto-registers accounts from transaction history

> **An accounts registry created on first use**, so existing data keeps working and the interface can offer a picker.

**What this step is doing.** Turning free-text account names on transactions into a known set of accounts.

**The problem.** Account names arrive as text typed on a transaction. Requiring registration up front breaks all your historical data. Not registering at all means a typo silently creates a phantom wallet holding real lots, which you then cannot sell from the account you meant.

**How it works.** The account record is created the first time a name appears, with a kind attached, and the migration backfills from existing data so nothing already recorded becomes invalid. The dashboard then offers the registry as a picker, so typos stop producing new wallets.

### 28. HarvestIQ implements basis-preserving inter-account transfers

> **Transfers carry the cost basis, the real purchase date, any inherited holding period, and a proportional share of consumed replacement capacity.**

**What this step is doing.** Letting you move holdings between your own accounts without the engine treating it as a sale.

**The problem.** Once sales are scoped to accounts, moving assets between your own accounts has to be modelled properly. A transfer realizes nothing, no gain and no loss, but the lots have to arrive carrying everything that determines how they will be taxed later. Get it wrong and either you cannot sell what you can plainly see you hold, or a transfer silently resets a long-term position's clock back to zero.

**How it works.** Whole lots are reassigned with everything intact. Partial transfers split the lot, and the moved shares keep the cost basis, the real purchase date, the inherited holding period where one exists, and a proportional slice of the consumed replacement capacity. That last one because the capacity is an attribute of the shares themselves rather than of where they happen to be custodied. Selection is oldest-first from the source account by default, or by lots you name. A transfer record is written as the paper trail, while the lot changes are the substance.

*Authority: no disposition occurs under IRC §1001, so nothing is realized, basis carries over unchanged under IRC §1012, and the holding period continues. Rev. Proc. 2024-28 is what makes the transfer consequential, by requiring the basis to be tracked per wallet once it arrives.*

### 29. HarvestIQ supports FIFO, HIFO and specific-lot identification

> **Because harvesting is lot selection**, and the default method is the one most hostile to it.

**What this step is doing.** Letting you control which lot a sale consumes, since that choice determines the tax result.

**The problem.** Oldest-first, the default when nothing is specified, sells your oldest shares. Those are frequently your lowest-basis shares, which is exactly backwards for a harvester who wants to sell the highest-basis shares, because those are the ones sitting at a loss.

There is an execution problem beneath the strategic one. A plan that names a specific lot is worthless if the system cannot sell that lot. Reproducing the recommendation by hand in a broker interface usually falls back to oldest-first, which produces a different taxable result from the one that was valued.

**How it works.** Three methods. Oldest-first, which is the default. Highest-basis-first, which realizes the largest loss or the smallest gain per share sold and is the tax-efficient default when you are not targeting a specific lot. And specific identification, which consumes exactly the lots you name in the order you give them.

Specific identification is allowed for both shares and crypto. That was never the difference between the two asset classes, contrary to a widespread belief, though for crypto the choice has to be made before the trade executes and documented in your own records.

*Authority: Treas. Reg. §1.1012-1(c) for specific identification of shares sold.*

### 30. HarvestIQ builds fee-inclusive cost basis with an oversell guard

> **Fees included, oversells rejected before anything is written, and no averaging across lots.**

**What this step is doing.** Getting the three basic facts of the ledger right at the moment a transaction is recorded.

**The problem.** Three requirements, each of which invalidates everything downstream if it is wrong.

Cost basis is what you paid including acquisition fees, and leaving them out overstates every loss you will ever compute from that lot. A sale larger than the position you hold has to be rejected rather than quietly absorbed. And a sale spanning several lots must not average their bases together, because each lot has its own basis and possibly its own classification, and each is reported separately on a return.

**How it works.** A purchase opens a lot at the total cost including fees, divided by the quantity. A sale sums the available quantity across the lots selected and rejects an oversell before anything is written, with a different message depending on whether you chose the lots or the engine did, because those are different mistakes to explain. Consumption then walks the lots in order and writes one realized record per lot, each carrying that lot's own basis, proceeds, and classification.

For example, an account holds 5 shares at a $180 basis and 5 at $190. Selling 8 at $200 realizes a $100 gain against the first lot and a $30 gain against the second, so $130 in total, recorded as two rows rather than one calculation against a blended $185 basis.

*Authority: IRC §1012, under which basis is cost.*

### 31. HarvestIQ uses the calendar anniversary rule for holding period

> **More than one year measured on the calendar**, so 1 February 2025 to 1 February 2026 is still short-term.

**What this step is doing.** Deciding whether each realized gain or loss is short-term or long-term.

**The problem.** Testing whether more than 365 days have passed implements a different rule and gives different answers. A lot bought on 1 February 2025 and sold on 1 February 2026 has been held exactly one year and is still short-term. 2 February is the first long-term day. Across leap years the day-count version misclassifies a whole day of boundary cases in either direction, and since short-term and long-term rates can differ by more than a factor of two, the difference is real money.

**How it works.** The anniversary is computed by advancing the year on the purchase date itself, with 29 February resolving to 28 February, and a sale is long-term only if it falls strictly after that date. Every classification reads the effective holding date, so wash-adjusted lots classify on their inherited age while lot ordering and the wash windows keep using the real purchase date.

*Authority: IRC §1222, which defines long-term as held for more than one year; Treas. Reg. §1.1223-1 for computation of the period.*

---

## STAGE 6 — WARNINGS: what silently voids a harvest

### 32. HarvestIQ implements a DRIP wash-sale guard

> **A reinvested dividend is a purchase and the wash-sale rule counts it**, which is the highest-volume complaint in the whole domain and trivially preventable.

**What this step is doing.** Warning you about a purchase your broker is going to make on your behalf, before you sell into it.

**The problem.** The characteristic case is someone who sells an entire position on 29 December and then finds the loss reduced on their year-end tax form, because a dividend reinvested on 16 December counted as a purchase inside the window. They did everything right as they understood it and still lost the loss.

The engine cannot see your broker settings and cannot stop the purchase. So detecting it afterwards is worthless. The warning has to arrive before the sale.

**How it works.** You declare, per holding, whether reinvestment is switched on and when the next or most recent distribution falls. If a distribution lands inside the thirty-day window either side of a proposed harvest, the guard fires with different advice in each direction, because they are different problems. A distribution that is coming prompts you to switch reinvestment off for that holding first. One that has already happened prompts you to check with the broker and record the reinvested shares, or to wait until thirty-one days after that date.

The asymmetry is deliberate. Dividend exposure warns but does not remove the candidate, whereas wash-sale exposure reflects purchases already made and removes it entirely. Dividend exposure is something you can fix in five minutes and then harvest. Folding them into one flag would either hide a fixable condition or block a legal harvest.

*Authority: IRC §1091(a). A dividend reinvestment is an acquisition for this purpose.*

### 33. HarvestIQ prices the seasoning deadline instead of flagging it

> **It values the same loss a second time as though already long-term and reports the difference**, warning only when waiting actually costs you money.

**What this step is doing.** Telling you when a short-term loss is about to become worth less, and by exactly how much.

**The problem.** A short-term loss that crosses its one-year anniversary becomes a long-term loss and is usually worth less, because a short-term loss offsets short-term gains first and those carry the highest rates. Unlike most timing questions in investing, this one has a hard date you can act on.

Concretely: a lot at 360 days carrying $4,000 of loss, with $10,000 of short-term and $10,000 of long-term gains already realized. Harvested today the loss cancels short-term gain at 37 percent and returns $1,480. Harvested a week later it can only reach the long-term pile at 15 percent and returns $600. You lose $880 to a date nothing was watching.

But a generic badge saying "converts in 12 days" is noise. Most of the time the conversion costs nothing, and warnings that fire when nothing is at stake teach people to ignore warnings in general.

**How it works.** The exposure is priced rather than flagged. The same loss is valued a second time as though it were already long-term, and the difference is reported in dollars. The warning fires only when that difference is positive.

The finding that came out of building it invalidated one of my own tests before it could reach a user. If you hold only short-term gains, a long-term loss cross-nets into them at the short-term rate anyway. So the classification change is worth exactly zero and the badge correctly stays down. Long-term gains have to be present and competing for the loss for any exposure to exist at all. That does not follow from the concept, and it only falls out of the netting sequence.

Deadlines are computed from the inherited holding period where one exists rather than the purchase date, so a lot that absorbed a disallowed loss reports its real anniversary. The figures ride on the plan's steps and, more importantly, on the positions the plan is deferring into next year, since those are the ones at risk of crossing unwatched.

*Authority: IRC §1222 for the classification that the deadline changes.*

---

## STAGE 8 — EXECUTION, INTEGRITY AND DISCLOSURE

### 34. HarvestIQ executes named-lot disposals from the plan

> **The plan names a specific lot and the interface sells that lot**, so the recommendation is something you can act on rather than something you have to translate.

**What this step is doing.** Closing the gap between a recommendation and a trade.

**The problem.** Harvesting recommendations are conventionally advisory. You read a suggestion and reproduce it by hand, at which point lot selection is usually out of your control. Since the whole valuation depends on which lot is sold, a recommendation you cannot execute against the intended lot will most likely produce a different taxable result from the one that was valued. The advice was correct and the outcome is not.

**How it works.** The dashboard is built around the plan as its main surface rather than around the holdings table. Each step shows the three figures and executes through a two-step confirmation that sells the exact lot named, at market. Partial steps display as a quantity out of the lot total and execute as sized rather than rounding up to the whole lot.

The surrounding surfaces carry the conditions that qualify a recommendation: wash-sale, permanent-forfeiture, dividend and seasoning indicators on the relevant rows, disallowed amounts shown against realized gains, and a capacity tile reporting the remaining pool alongside the portion currently blocked. Settings for carryforwards, expected future rate, the dial, outside accounts and dividend declarations are all exposed directly, because every one of them changes the valuation and none can be derived from your transaction history.

### 35. HarvestIQ writes the ledger atomically

> **One save per transaction**, so a failure part-way through cannot leave lots reduced with no realized gains recorded against them.

**What this step is doing.** Making sure a recorded transaction is all-or-nothing.

**The problem.** Recording a sale writes a transaction record, reduces one or more tax lots and possibly closes them, and writes one realized gain record per lot consumed. The original implementation saved to the database at three separate points inside that sequence.

Each save is a durable boundary, so a failure between any two of them leaves the database in a state that is internally inconsistent and cannot be fixed by retrying. You get a transaction recorded with no lot consumption, or lots reduced with no realized gains against them, or a sale applied to some of the lots it should have consumed and not others.

And because every downstream figure reads those records, an inconsistent ledger raises no error at all. It produces confident, wrong tax figures indefinitely, with nothing to indicate that anything failed.

**How it works.** The intermediate saves became flushes, which assign database identifiers so dependent records can reference the transaction without creating a durable boundary, and a single save now happens at the end. The whole set of records either persists together or does not persist at all.

This ordering is also what later made transaction-time wash-sale enforcement possible. Both directions modify realized gains and replacement lots written moments earlier in the same operation, and they are only safe to run inside it because the operation is atomic. Had the original boundaries stayed, enforcement would have had to become a separate reconciliation pass over records that were already durable and already wrong.

### 36. HarvestIQ stores the tax-reportable gain as the primary figure

> **The main gain field holds the figure after disallowance**, and a second field holds the disallowed portion.

**What this step is doing.** Deciding which of two correct answers gets stored where.

**The problem.** After a wash sale there are two true answers to what the loss was. The economic loss is what actually happened to your money. The tax-reportable loss is what goes on the return. Storing only one throws away information, and storing them in the wrong roles forces everything reading them to adjust.

Brokers do not even agree on this. One major broker's year-end form reports gain and loss before wash adjustment and another reports it after, with no industry standard between them.

**How it works.** The primary field holds the reportable figure, and the economic result is recovered by adding the disallowed portion back. Keeping the tax figure primary is what lets year-to-date totals, the netting sequence, and a future tax-form export all read one field without adjusting it.

One consequence had to be handled explicitly. A fully disallowed loss has a reportable figure of exactly zero. So the wash-sale report selects on the economic loss, meaning cost basis exceeding proceeds, rather than on the reportable figure. Selecting on the reportable figure would make the most severe wash sales, the ones disallowed in full, vanish from the wash-sale report entirely.

*Authority: IRC §1091(d), under which the disallowed loss is added to the basis of the replacement shares.*

### 37. HarvestIQ uses exact decimal arithmetic end to end

> **Exact decimals throughout, and money crosses the interface as text.**

**What this step is doing.** Protecting the number every other calculation depends on.

**The problem.** Cost basis feeds everything. A basis figure that round-trips through a floating-point representation comes back very slightly wrong, and that error then propagates into realized gains, wash-sale disallowances, and lot splits. It does so silently, and permanently, because the result gets written back to the database.

Separately, a tax rate is a fraction. Someone entering 40 to mean 40 percent should get an error rather than a confidently wrong answer a hundred times too large.

**How it works.** Exact decimal types in both the database and the application, with money and quantities serialized as text across the interface so no client can round them through a floating-point number even by accident. Stored figures are normalized so they read cleanly without losing precision where it matters. Rates are bounded between zero and one at the boundary. The background worker cannot serialize decimals across its queue, so rates cross that boundary as text and get reconstructed exactly on the other side.

### 38. HarvestIQ eliminates the N+1 query in the opportunity builder

> **One pass per symbol for wash-window lots and one lookup table for securities**, instead of a database round-trip for every lot you own.

**What this step is doing.** Making the analysis cheap enough to run often.

**The problem.** The original opportunity builder had a classic N+1 query pattern. One query fetched the lots, and then two more queries ran per lot: one for that lot's security record, and one for the window purchases on its symbol. On a portfolio of any size that becomes a cascade of round-trips, on a code path that runs on every recompute, meaning every thirty seconds and on every request to the live endpoints.

The correctness stakes are indirect but real. An analysis that is expensive to compute gets computed less often, and a harvest priced against a stale position is a harvest priced wrongly.

**How it works.** Securities are loaded once into a lookup table keyed by symbol. The wash-window lots are gathered in a single pass into a table keyed by symbol before the per-lot loop starts, and the outside-account activity is loaded in one joined query. The capacity calculation also accepts already-loaded lots and securities from callers that hold them, which is how the live summary path computes capacity at no additional query cost at all.

### 39. HarvestIQ caches the analysis on a background worker

> **Two cadences for two costs.** Prices stream every second; the tax analysis recomputes on an interval and is read from cache.

**What this step is doing.** Deciding what gets computed when, given that two things in the product move at very different speeds.

**The problem.** The full analysis values every open lot, evaluates wash exposure across internal and outside accounts, and then simulates the sequenced plan. That is expensive relative to a page load, and it changes whenever prices move. Computing it on every request makes the interface slow. Computing it on every price tick is waste, because prices update roughly once a second while your tax position moves far more slowly than that.

**How it works.** The worker computes the opportunities and the plan together and writes both into a single cache entry with a timestamp. Read paths never recompute.

Caching both together is what makes capability 9 possible, because the summary can take its headline from the sequenced plan while the interface still shows the per-lot list for inspection. Endpoints that queue work return an accepted status with a job identifier and a location header pointing at it. Asking for the analysis before anything has been computed returns a service-unavailable status rather than a success response carrying an empty body, which every client would otherwise have to inspect to discover whether it actually got an answer.

### 40. HarvestIQ confines the language model to narration

> **The model reads the finished analysis and explains it.** It produces no figures.

**What this step is doing.** Making the plan understandable to someone who does not know the tax rules, without letting the explanation become the calculation.

**The problem.** The obvious use of a language model here is to reason about the tax position, and that is exactly the wrong division of labour. These calculations are deterministic, auditable and legally consequential. A model that recomputes them introduces variance into numbers that must not vary.

**How it works.** The explanation task reads the already-computed analysis out of the cache and builds its prompt from it, so the model is narrating a decision the engine has already made. The prompt carries each position's actual stated wash-sale reason and instructs the model not to substitute vaguer phrasing for it, and to close with exactly one line stating that this is not personalized tax advice.

It runs on a background worker rather than inside the web process, because a model call takes seconds and holding a web worker for that long is what makes an application feel dead. The endpoint is a POST specifically because it costs money and is not idempotent, so it must not be triggerable by a browser prefetch or a retry.

### 41. HarvestIQ discloses its rate-model limitations

> **The harvesting logic is rigorous; the tax rate model is a stated simplification you supply.**

**What this step is doing.** Making sure the reported figures are read for what they are.

**The problem.** The two principal regulatory actions in this sector, against Wealthfront in 2018 and Betterment in 2023 with a $9 million penalty, were both about overstating the benefit that automated harvesting delivered. Wealthfront told clients it monitored for wash sales while wash sales occurred in at least 31 percent of enrolled accounts. In this domain an unqualified dollar figure is the liability, which makes disclosure a correctness requirement rather than a matter of tone.

There was also a specific temptation available. Citing a published tax-alpha estimate of 1 to 2 percent a year from an asset manager's research would have made the product sound considerably stronger.

**How it works.** No third-party point estimates appear anywhere in the product. Published lifetime estimates span roughly 0.15 to 1.1 percent a year depending almost entirely on whether the study debits the future tax created, and picking one number out of that range is exactly what both enforcement actions were about. The limitations are instead documented as specifically as the features:

- **Flat rates, no bracket calculation, no 0/15/20 percent thresholds.** Someone inside the zero-percent long-term band, roughly under $49,000 single or $99,000 joint for 2026, is told a long-term harvest saves 15 percent. It saves nothing, and it is worse than nothing, because they have lowered their basis against a future taxable gain.
- **No investment surtax**, including the counter-intuitive part where net losses count against investment income only up to the same $3,000 limit.
- **No special rate groups.** Collectibles at 28 percent, which includes physically backed metals funds such as GLD and SLV, and certain real-estate recapture at 25 percent, are all treated as ordinary long-term.
- **No alternative minimum tax and no state treatment.** Pennsylvania breaks the engine outright, with no carryforward, no $3,000 offset, no short-term or long-term distinction, and the federal wash-sale provisions not applying at all for state purposes. New Jersey provides no carryforward either.
- **No threshold modelling.** Medicare premium surcharges, ACA subsidy cliffs and the Social Security taxable-income phase-in all interact with capital losses, and none of them are represented.
- **Carryforward carries no present-value or mortality discount.** Unused carryforwards expire at death.
- **The mandatory-use provision is not modelled.** The law requires losses to be used to the maximum extent in the current year, with no election to defer. Someone with no gains and income below the standard deduction destroys $3,000 of banked capacity for nothing, and the engine still recommends the harvest.
- **Not modelled:** the six-month rule where a capital-gain distribution reclassifies a short-term loss as long-term, the 61-day rule where selling shortly after an ex-dividend date converts that dividend from preferential to ordinary rates, and return-of-capital distributions that reduce basis.
- **Regimes that invalidate the engine entirely** for the person affected: mark-to-market traders, who are exempt from the wash-sale rule and have no $3,000 limit, plus straddles and constructive sales.
- **No volatility, correlation or beta anywhere.** Nothing is risk-adjusted. Price history is fetched for charting and as a quote fallback and never enters a tax or selection calculation. Any claim that the engine is volatility-aware would be false.
- **"Substantially identical" is a placeholder, not a feature.** Replacement suggestions are same-sector, different-symbol, with no correlation or tracking-error screening, which follows directly from the point above. The tax authority has never defined the term. The defensible product is a documented rationale with a stored audit trail rather than a determination, and it is not built.
- **Not multi-user.** No authentication and all data shared, which is the blocker to being genuinely real.
- **Tests run on an in-memory database** rather than the production one. Transfer fees are not added to basis. Currency is stored but unused in profit and loss.

### 42. HarvestIQ keeps its public description synchronized with the engine

> **The stated limitations describe the engine that exists**, not the one that existed when they were written.

**What this step is doing.** Making sure the description a reader encounters first is actually true of the software.

**The problem.** Documentation drifts in one direction and it is the damaging one. A stated limitation gets written once, the code moves past it, and nobody revisits the sentence, because nothing breaks when a limitation goes out of date and no test covers prose.

The project's own limitations list had drifted far enough to be actively misleading. It disclosed the absence of wash-sale cost-basis adjustment, single-account-only wash checks, a day-count holding rule, per-position estimates that do not sum, and an approximate loss budget. All five had shipped. The engine was being described as deficient in precisely the five areas where it had become strongest, and anyone assessing the project read a list of weaknesses that no longer existed with nothing about the capabilities that replaced them.

The failure runs in both directions, and understatement is the less obvious one. Overstating a benefit attracts enforcement. Understating a capability costs nothing legally and everything in assessment.

**How it works.** The introduction now leads with the engine and is framed around deferral rather than saving, so the worked example carries all three figures instead of stopping at the headline. The limitations list was replaced wholesale with the real set, which is the one in capability 41.

The resulting claim is both stronger and defensible: the harvesting logic is rigorous, and the rate model is a stated simplification you supply. That is a more precise position than a vague list of caveats, and it is the position the enforcement actions in this sector were about.

**Known incomplete.** The rewrite corrected the main document but not every surface. The interactive API description still advertises FIFO-only lot selection and per-position savings estimates, both false since 8 August when highest-basis-first, specific-lot selection and the sequenced plan all shipped. The same defect this capability is about is therefore still live on a second surface, and is recorded here rather than quietly carried.

---

# ROADMAP

1. **Replace the flat-rate model** with real bracket stacking and the investment-income surtax. The before-and-after architecture is already shaped for it, making this a contained substitution rather than a rewrite, and the single change that improves every figure in the product at once.
2. **Refuse to recommend in the zero-benefit cases**: the 0% long-term band, income below the standard deduction, and positions already past the limitation with no foreseeable gains.
3. **The six-month capital-gain-distribution check**, which is nearly free given that distribution dates are already tracked.
4. **Tax-form output with wash-sale adjustment codes**, plus a reconciliation view against the broker's own year-end form. This is largely an export layer over data that already exists, and material because those adjustment codes remove the shortcut summary reporting that ordinary portfolios get.
5. **Capacity snapshots over time**, so the decay curve can be shown rather than a single current figure.
6. Then: state conformity with the two states that break the model handled as special cases, present-valued carryforward with a mortality discount, alternative minimum tax, and threshold modelling behind explicit user declarations.

---

# CHANGE RECORD

*This record was compiled from the code and the project documentation, then verified against the repository history commit by commit. Every capability above traces to a commit below; every functional commit below traces to a capability. Where the two disagreed, the code was treated as authoritative and the discrepancy is noted.*

## 16 June – 6 August 2026 — the foundation

Two streams. The **ledger stream** established the accounting substrate; much of it stands unmodified. The **infrastructure stream**, the web framework, database, migrations, background worker, the move from polled prices to a streaming market-data feed, and the live dashboard, is engineering rather than tax logic and belongs to Deliverable 3.

| Date | Change | Status |
|---|---|---|
| 20–21 Jun | Transaction persistence and retrieval; holdings endpoint | Superseded, holdings later rebuilt from lots |
| 4 Jul | Date and exact-decimal column types, fractional quantity | Retained, precursor to capability 37 |
| 4 Jul | Transaction validation; security master | Retained |
| 4 Jul | **Tax lots with fee-inclusive cost basis** | Retained (cap. 30) |
| 4 Jul | **Oldest-first disposal with an oversell guard** | Retained and extended (cap. 29, 30) |
| 4 Jul | **Realized gains with short/long classification** | Retained; rule corrected 8 Aug (cap. 31), field redefined (cap. 36) |
| 4 Jul | Unrealized profit and loss against manual prices | Retained; prices later streamed |
| 4 Jul | **Wash-sale detection over a ±30-day window** | Rewritten 8 Aug on both axes, the test (cap. 15) and the effect (cap. 16) |
| 4 Jul | **Harvest ranking by estimated savings** | Replaced 8 Aug, was loss × rate, priced independently (cap. 1, 4, 8) |
| 4 Jul | **Crypto asset class with wash-sale exemption** | Retained (cap. 25) |
| 4 Jul | Sector-based replacement suggestions | Retained, still a placeholder (cap. 41) |
| 4 Jul | AI harvest explanation | Retained; moved to the worker 12 Jul (cap. 40) |
| 4 Jul | Migrations assume schema control | Retained, every tax change since ships as a migration |
| 4 Jul | Test suite: lot ordering, basis, realized gains, wash sales, crypto | Base of the current 63 engine tests |
| **12 Jul** | **Non-atomic ledger writes corrected** | Retained (cap. 35) |
| **12 Jul** | **Query-per-lot eliminated from the opportunity builder** | Retained and extended (cap. 38) |
| 12 Jul | Price refresh and AI explanation moved to the worker | Retained (cap. 40) |
| **17 Jul** | **Lot matching scoped by account** | Retained (cap. 26) |
| **18 Jul** | **Savings netted against gains realized so far this year** | Superseded 8 Aug (cap. 1, 5) |

Four of these are tax decisions rather than scaffolding. **Atomic ledger writes (12 July)** is load-bearing for everything in August that modifies records inside a transaction. **The query-per-lot fix (12 July)** established the load-once pattern that capability 38 later extended to the live path. **Account-scoped matching (17 July)** is the origin of per-wallet basis, built three weeks before the regulatory research, because drawing on a lot held somewhere else is simply the wrong lot; the rule turning out to mandate it was confirmation, not cause. **Netting against this year's realized gains (18 July)** was the first move away from flat-rate valuation and identified the right question three weeks before the full sequence replaced it.

## 8 August 2026 — the tax engine overhaul

Entering the session: each losing lot valued independently at loss × rate against gains realized so far; wash sales detected by the presence of any recent purchase and enforced nowhere; oldest-first disposal only. Basis was already account-scoped from 17 July.

The session shipped as **nine functional commits over roughly two hours**, in this order:

| # | Commit | Capabilities delivered |
|---|---|---|
| 1 | Update the tax math | 1–6, 8, 9, 13, 39 |
| 2 | Enforce the wash-sale rule, add highest-basis and specific-lot selling | 15, 16, 17, 29, 31, 36 |
| 3 | Add the dividend-reinvestment wash-sale guard | 32 |
| 4 | Add watch-only external accounts for cross-account wash sales | 22, 23, 24 |
| 5 | Add honest deferral math, saves now, costs later, net | 7 |
| 6 | Rebuild the dashboard around the harvest plan | 34 |
| 7 | Fix wash-sale tacking: split replacement lots, separate the holding date | 18, 19, 20 |
| 8 | Price carryforward explicitly, size partial sells, fix spent-share washes | 10, 11, 21 |
| 9 | Add multi-wallet: accounts registry and basis-preserving transfers | 27, 28 |

Broken out by change rather than by commit:

| Change | Capability |
|---|---|
| The statutory netting sequence and the $3,000 limitation | 1 |
| Negative liability permitted for the ordinary-income deduction | 2 |
| Short-term-first ordering of the ordinary offset | 3 |
| Valuation as a before-and-after difference | 4 |
| Carryforward settings and the effective position | 5 |
| Savings separated from carryforward generated | 6 |
| Deferral accounting, saves now, costs later, net | 7 |
| Sequenced plan replacing the ranked list | 8 |
| Headline drawn from the plan rather than the per-lot sum | 9 |
| Partial sizing and the carryforward dial | 10 |
| A single explicit ranking score with tie-breaks | 11 |
| Plan reports its exclusions, deferrals and reason for stopping | 13 |
| Detection rewritten to replacement shares retained; safe-to-repurchase date | 15 |
| Wash-sale rule moved from advisory detection to ledger enforcement | 16 |
| Both halves of the window enforced | 17 |
| Replacement lots split share by share | 18 |
| Holding-period transfer corrected to once per matched share | 19 |
| Inherited holding date separated from the real purchase date | 20 |
| Replacement shares made single-use | 21 |
| Watch-only external accounts | 22 |
| Retroactive washing when an external purchase is recorded | 23 |
| Permanent forfeiture in tax-advantaged accounts | 24 |
| Accounts registry | 27 |
| Basis-preserving transfers | 28 |
| Highest-basis-first and specific identification | 29 |
| Calendar holding-period rule | 31 |
| Dividend-reinvestment guard | 32 |
| Dashboard rebuilt around the plan with named-lot execution | 34 |
| Analysis cached on the worker with opportunities and plan together | 39 |

Four of these, splitting replacement lots, separating the holding date, single-use replacement shares, and carryforward pricing with partial sizing, originated as findings from my audit rather than as planned features.

**Two further commits landed in this block but are not tax-engine work** and are recorded here only so the history reconciles: the dashboard's visual redesign (warm paper theme, new typography, and the overview grid reflowed to accept a fifth tile), and the process file that starts the web server, worker and price streamer from one terminal. Both belong to Deliverable 3.

**One capability has no commit of its own.** Capability 13, the plan reporting its exclusions, its deferrals and its reason for stopping, shipped inside commit 1 alongside the plan itself. It is listed separately because it is a distinct product decision, but it will not appear as a separate line in the history.

**Tests:** 52 engine tests plus 5 websocket tests, 57 in total, at session close.

## 9 August 2026 — the tax alpha decay review

Prompted by a J.P. Morgan Private Bank analysis arguing that harvesting decays as basis ratchets downward, with regular cash contributions as the remedy. The scoping conclusion: the analysis contains exactly one strategy, contribute new capital so that fresh high-basis lots exist to harvest later. Decay is the diagnosis, not the strategy; contribution frequency and lump-sum-versus-gradual are parameters of it. The engine's real exposure was treating unrealized loss as costless, which the previous day's partial sizing had already corrected. What remained was one defect and two features.

| Change | Capability |
|---|---|
| Sizing dial corrected from a binary switch to a continuous function | 12 |
| Harvest capacity: the pool, the blocked split, drawdown-to-harvest | 14 |
| Seasoning deadline, priced rather than flagged | 33 |
| Efficiency-ranking recommendation closed by verification, no change made | 11 |
| Contribution planner assessed and deliberately excluded | 14 |
| Crypto decay caveat, ossification by appreciation alone | 25 |
| Decision not to cite third-party tax-alpha estimates | 41 |
| Public description resynchronized with the engine; stale limitations replaced | 42 |

Three functional commits, in order: make the carryforward dial size sells continuously rather than as an on/off switch; add harvest capacity; warn on lots about to season from short-term to long-term. The remaining commits that day were documentation.

No migration required. **Tests:** 63 engine tests at session close, up from 52, plus the 5 websocket tests, which require a live cache server and are run separately.

**A discrepancy found while verifying this record.** The project's own status notes report "57 tests" for 8 August and "63 tests" for 9 August, which reads as a gain of six. The two figures are counted differently: 57 counts both test files, 63 counts the engine file alone. Counted consistently, the engine file went from 52 to 63, a gain of eleven, and the true totals are 57 and 68. The figures above use the consistent counting.

---

*Deliverables 2 (résumé bank) and 3 (build logs, systems and engineering) to follow.*
