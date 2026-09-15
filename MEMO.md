# Memo: Crash-to-Contact

**Subject:** Daily crash-derived lead feed — findings and recommendation
**Date:** 2026-09-15

---

## The short version

The pipeline works. It ingests three public crash sources, conforms them, enriches them
geospatially, scores them, and attaches a defensible eligibility decision to every record.

**It produces zero leads that a live outbound call centre may lawfully contact — in Maryland,
Texas and Florida alike.** Not few. Zero. That is not a pipeline failure and it is not
conservatism; it is what the law says, and four independent bars each produce it on their own.

The engineering was the easy part.

---

## 1. What was built, and what was deliberately not

**Built.** Three sources ingested with incremental watermarks and byte-for-byte raw retention:
Montgomery County (125,172 crashes), TxDOT (14,535), NHTSA FARS (222,695). Conformed to one
crash fact, enriched with coordinate-derived timezones, census geography, road-network snapping
and H3 indexing. A versioned compliance engine, an explainable lead score, an orchestrated
graph, data contracts at every layer boundary, and a drift detector.

**Deliberately not built, each for a reason:**

- **Census socioeconomic data (ACS).** Block-group income is a close proxy for protected
  characteristics, and ranking contact priority on it is a redlining-adjacent design. It is not
  ingested at all — a policy against misuse lasts only as long as institutional memory of it,
  whereas not holding the data is enforceable by the schema. Cost: the population-normalised
  hotspot contrast the brief asks for cannot be shown, and the document says so.
- **Road networks for Texas and Florida.** 1.3 GB of additional extracts to demonstrate a
  technique already demonstrated on the densest available data.
- **A full weather backfill.** Open-Meteo's free tier meters by data volume and refused the
  request. Bounded rather than abandoned.

### The Texas source needs a decision, not just an ingestion

The TxDOT layer is titled and described as *bicyclist-involved crashes*. It is not. Querying it
without a filter returns 3,088,450 records against 14,535 that actually carry the bicyclist
flag, and the service's own description string is `txdot_ph_2.automated_sw.cris_crash`. **It is
publishing the entire statewide CRIS crash table.**

It is genuinely public, unauthenticated, and covered by no login, paywall or robots
restriction. Nothing had to be circumvented to read it. The likeliest explanation is a
misconfigured publication rather than a deliberate release.

**The position taken: ingest the labelled scope, treat the wider table as available but not
authorised.** The pipeline defaults to `bicyclist_involved_fl=1` — what the layer says it
publishes — and the full table requires an explicit flag plus a second confirmation for a pull
that size. The reasoning is that "technically reachable" and "intended to be published" are
different claims, and only the second is a basis a business can stand behind. A publisher
correcting the misconfiguration would silently cut the wider scope by 99.5%, so anything built
on it rests on someone else's mistake persisting.

This costs volume, and the cost is real: 14,535 Texas crashes instead of 3.09 million. It buys
a provenance story that survives the publisher fixing the error — which matters more here than
elsewhere, because §2721(c) requires stating the acquisition basis for every record, and
"it was exposed by accident" is not one.

Worth being clear that this changes nothing about the answer in section 3. Texas strips contact
identity from this product by statute either way, so the wider scope would add rows, not
contactable people.

## 2. Volume, and the exclusion table

The brief asked for the exclusion table on the grounds that it is more informative than the
inclusion table. It is.

**Contactable leads per day, live outbound telephone: zero, in every jurisdiction.**

Of 40 test identities, all 40 excluded:

| Count | Reason |
|---|---|
| 40 | `LIVE_SOLICITATION_PROHIBITED` |
| 28 | `MD_MVA_TELEPHONE_SOLICITATION_BAR` |
| 6 | `TX_BARRATRY_FELONY` |
| 3 | `DPPA_NO_PERMISSIBLE_USE` |
| 3 | `CONSENT_ABSENT` |
| 2 each | `CONSENT_REVOKED`, `DNC_LISTED`, `FL_CRASH_REPORT_60D` |
| 1 each | `LINE_TYPE_UNRESOLVED`, `RND_NO_DATA_NO_SAFE_HARBOR`, `RND_REASSIGNED`, `DNC_SCRUB_STALE` |

Reasons stack. Removing any single bar changes nothing.

**A note on volume that matters more than the table.** The underlying crash feed is healthy —
Montgomery alone produces about 29 crashes a day. The constraint is not supply.

## 3. Can these records lawfully go to a live outbound call centre?

**No. For Maryland, Texas and Florida the answer is zero — there is no compliant path for this
product as described.**

Four independent bars, any one sufficient:

**The data may not be used for solicitation.** DPPA §2721(b) permits disclosure of motor
vehicle record information for fourteen enumerated uses. Solicitation appears only at (b)(12),
and only where the *State* obtained consent — no state runs such a program. *Maracich v.
Spears* (2013) forecloses the litigation workaround by name. Exposure is **$2,500 per record**
as a statutory floor, so this is per-row liability.

**Live telephone solicitation is barred.** ABA Model Rule 7.3(b) and its analogues in all
three states prohibit solicitation by live person-to-person contact, and the ABA comment
defines that to include live telephone.

**In Texas it is a felony that reaches the caller.** Tex. Penal Code §38.12(a)(2) makes
telephone solicitation for economic benefit barratry — a **third-degree felony** attaching to
the individual dialling, not only the firm. In Florida, knowing misuse of crash data is
separately a third-degree felony under §316.066(3)(d).

**Maryland bars it by name.** Gen. Prov. §4-320 expressly prohibits using MVA personal
information for telephone solicitation.

### And a structural fact underneath all four

**None of the three sources contains callable identity.** Texas strips it by statute —
§550.065(f) removes name, address other than ZIP, and telephone from the only bulk-accessible
product. Montgomery publishes an opaque person GUID. Maryland's person table is titled
"(Anonymized)". FARS carries no names.

That is not an oversight in the data. **It is the statutory design.** Even setting every
prohibition above aside, there is nothing in these feeds to dial. The 40 identities in this
analysis come from a supplied test fixture that exists precisely because the real join is
unavailable — and buying that identity layer from a broker does not cure the DPPA problem, it
relocates it.

## 4. What product shapes remain viable

**Aggregate analytics — viable now.** Nothing above restricts aggregated, non-identifying
analysis, and §2725(3) expressly excludes ZIP5 from "personal information", which is the
legitimate aggregation key. The pipeline already produces statistically significant crash
hotspots, road corridors with elevated injury rates, and space-time clusters. Sellable to
municipalities, insurers and road authorities without touching an individual.

**Consented inbound — viable, and slower.** If the consumer initiates contact and consent is
captured with full provenance, the DPPA and solicitation problems both fall away. This is a
different business: marketing spend and conversion funnels rather than a data feed.

**Non-solicitation uses under an enumerated DPPA exception — narrow but real.** §2721(b)
permits use for insurance claims investigation, and for research. Neither supports outbound
sales.

**Written communication — the honest caveat.** Switching the ruleset to `written` produces 25
eligible leads of 40, because the bar rules gate *timing* for mail rather than prohibiting it.
But those 25 rest on a consent flag with no provenance behind it — no disclosure text, hash,
URL, timestamp or seller chain. Turning on the provenance requirement that production would
demand takes it to **zero**. Treat 25 as the ceiling under assumptions that cannot currently be
evidenced.

## 5. What to build next, given a quarter

**Consent infrastructure, first and mostly alone.** It is the only investment that changes the
answer to section 3. Capture at the point of consumer interaction, with the full provenance
chain, an append-only revocation ledger, and the cascade the FCC's revoke-all provision
requires by January 2027.

Second: productise the aggregate analytics, which are already built and currently a by-product.

Third: extend road enrichment to Texas and Florida, which is a day of compute and unlocks the
corridor analysis in the other two jurisdictions.

The quarter should **not** go on more sources. More crash data does not make an uncontactable
lead contactable.

## 6. Where this is most likely wrong

**The scoring model is barely better than chance.** ROC AUC 0.574 under spatial
cross-validation, against a 2.28% base rate. Crash-scene conditions do not predict injury
severity well, which is plausible — severity is driven by impact dynamics and occupant factors
that no public feed carries. It is weighted at 0.10 accordingly. *How to find out:* a holdout
against real case outcomes, which are not available here.

**The legal reading is an engineering reading, not a legal opinion.** Four independent bars is
a robust finding, but the *scope* of ABA MR 7.3 as applied to a non-attorney call centre is the
weakest link in the chain and deserves review by counsel. If that bar fell, three would remain.

**The cross-source overlap is quantified but not resolved.** 97% of Montgomery fatal crashes
and 98% of TxDOT fatal crashes also appear in FARS. Any count over the union double-counts by
roughly 637 crashes. Documented; deduplication is not yet implemented.

**Weather was never backfilled**, and Texas and Florida have no road enrichment. Both are
absent rather than wrong — every affected field emits null rather than a guess.

**The first place to look if something is wrong:** the severity crosswalk. Three sources use
three incompatible injury scales, and TxDOT's coded values are **not** ordinal — code 5 means
*no injury*, not most-severe. That mapping was derived from the injury-count columns rather
than assumed, and a naive reading would have inverted the ranking silently. If a number
downstream looks wrong, that is the most likely place it started.

---

*Supporting detail: `DATA_QUALITY.md` (defects and detection), `COMPLIANCE.md` (the ruleset
with citations), `ANALYSIS.md` (spatial analysis and backtest), `OPERATIONS.md` (orchestration,
storage, cost), `DECISIONS.md` (design decisions and rejected alternatives).*
