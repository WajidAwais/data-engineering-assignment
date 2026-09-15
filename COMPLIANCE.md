# Compliance

The contact-eligibility ruleset as prose, with citations. The executable form is
`compliance/rules.yaml` and `config/blackout_windows.csv`; this document explains what those
files say and why.

**Default disposition is INELIGIBLE.** Eligibility is proven per record, with a citation,
never assumed. No legal constant lives in Python — a test walks the engine's syntax tree and
fails if a statute number or time window appears in executable code.

*This is an engineering reading of the law, not legal advice. It is set out so the
reasoning can be checked, not relied on.*

---

## 1. Source eligibility — applies before any channel question

### DPPA, 18 U.S.C. §§2721–2725

Personal information from state motor vehicle records may be disclosed only for fourteen
enumerated permissible uses. **Solicitation appears only at §2721(b)(12)**, and only where
*the State* has obtained affirmative express consent. No state runs such a program at scale.

The obvious workaround is foreclosed by name: ***Maracich v. Spears*, 570 U.S. 48 (2013)**
holds that an attorney's solicitation of clients is not covered by the §2721(b)(4) litigation
exception. Penalties under §2724 are actual damages with a **$2,500 statutory floor per
record**, plus punitive damages and fees — so this is a per-row liability, not a per-incident
one.

`DPPA_NO_PERMISSIBLE_USE` fires unless affirmative record-level consent is present.

### The one carve-out relied on — §2725(3)

The 5-digit ZIP is **expressly excluded** from the definition of "personal information". That
is the legitimate path to geographic aggregation, and it is built into the schema rather than
written into a policy: `geo.zip5` survives into the analytic record and the output contract,
while name, street address and phone go to a segregated vault (`vault_party_identity`) and
the analytic side joins on an HMAC surrogate.

The same boundary governs the scoring model. `census_tract` and `census_bg` are held because
the output contract requires them, but `FORBIDDEN_FEATURES` in `src/scoring/features.py`
**raises** if any geographic or demographic identifier reaches the feature matrix. Holding
and ranking are different acts; the second is the redlining-adjacent design Part 4 warns
about.

### Texas — Tex. Transp. Code §550.065

The only bulk-accessible product is the **redacted** CR-3 required by §550.065(c-1).
Subsection (f) strips name, driver's licence number, date of birth other than year, address
other than ZIP, telephone number, plate number and insurer details.

**What that means for a contact pipeline, plainly: there is nothing to call.** The Texas feed
is structurally incapable of producing a contactable lead, by statute rather than by accident.

### Maryland — Md. Code Gen. Prov. §4-320

Maryland's DPPA analogue. It requires written consent for marketing lists and **expressly
bars use of the personal information for telephone solicitation**. `MD_MVA_TELEPHONE_SOLICITATION_BAR`.

---

## 2. Jurisdictional blackout windows

Implemented as a data table — `(jurisdiction, record_type) → earliest_contact_date` — not as
branches. Adding a jurisdiction is a CSV row plus a version bump, which is the property Part 5
says will be tested live.

| Jurisdiction | Rule | Window | Anchor |
|---|---|---|---|
| FL | Fla. Stat. §316.066(2) — crash reports revealing personal information are exempt from disclosure | **60 days** | filing date |
| FL | R. Reg. Fla. Bar 4-7.18(b)(1)(A) — targeted written communication re: an accident | 30 days | incident date |
| TX | Tex. Penal Code §38.12(d)(2)(C) — written solicitation re: an accident | 31 days | incident date |
| MD | Md. Rule 19-307.3 with Gen. Prov. §4-320 | **0 days** | incident date |
| US | 49 U.S.C. §1136(g)(2) — aviation accidents | 45 days | incident date |

**Where several rows match, the longest binds.** Florida is the worked example: the 60-day
data gate outlasts the 30-day bar rule, so a pipeline implementing only the bar rule has
implemented the wrong constraint.

**Maryland's zero is not "no restriction".** Maryland imposes no accident-specific waiting
period — the constraint is on the *method*, not the clock. A zero-day row is the correct
encoding and the notes column says so, because the alternative (a blank row) reads as
unresearched.

**A second-order effect worth naming:** because of §316.066(2), the most recent 60 days of
any Florida public feed is **structurally incomplete**. A trailing-30-day Florida trend reads
a statute as an outage. Anyone computing one needs to know that.

---

## 3. Channel gates — where the answer is decided

Ruleset v1.1.0 models `live_telephone`, because that is the channel the business asked for.
Three categorical bars attach to it, and any one is sufficient.

**Live person-to-person solicitation is barred.** ABA Model Rule 7.3(b) and its state
analogues — **Md. Rule 19-307.3**, **R. Reg. Fla. Bar 4-7.18(a)**, **Tex. Disciplinary R.
7.03** — prohibit solicitation by live person-to-person contact, and the ABA comment defines
that to **include live telephone**.

**In Texas it is a felony.** Tex. Penal Code **§38.12(a)(2)** makes in-person or telephone
solicitation for economic benefit barratry, a **third-degree felony** — and it reaches the
caller personally, not only the firm employing them. A dialler operator carries this risk
individually.

**In Florida, misuse of the data is also a felony.** Fla. Stat. §316.066(3)(d) makes knowing
misuse of crash data obtained under the confidentiality exemption a third-degree felony.

Because these are properties of the **ruleset** rather than the code, switching
`channel: live_telephone` to `written` and bumping the version produces a materially
different answer with no Python touched. That is both the demonstration Part 5 asks for and
the evidence behind the memo's product-shape analysis.

---

## 4. Contact-channel gates

### Calling window — derived from geography, never from the phone number

16 C.F.R. §310.4(c) and 47 C.F.R. §64.1200(c)(1) restrict outbound calls to **08:00–21:00
local time at the called party's location**. Several states are stricter: **Fla. Stat.
§501.616(6)** closes at **20:00**, as do Oklahoma and Washington. The stricter binds.

The window is computed from a **coordinate-derived IANA timezone**. That is not a stylistic
preference — measured in this data, a state-based rule is wrong for **2,030 crashes**: 819
Texas crashes resolve to `America/Denver` (El Paso and Hudspeth) and 1,211 Florida crashes to
`America/Chicago` (the western Panhandle).

Number portability and VoIP make NPA-NXX unreliable evidence of physical location, so it is a
**fallback and a cross-check only**, and where the two disagree the **intersection** is taken.
A pipeline computing this from `SUBSTR(phone,1,3)` fails the section outright.

### DNC — a freshness SLA, not a one-time load

The safe harbour at **16 C.F.R. §310.4(b)(3)(iv)** requires a registry version obtained **no
more than 31 days** before the call. Modelled as a staleness constraint with an automatic hold
when the scrub ages out (`DNC_SCRUB_STALE`). **A missing scrub date is treated as stale** —
absent evidence of a scrub is not evidence of a fresh one.

### The internal list, and the one exemption that touches it

Two further rules are enforced, and the relationship between them is the point.

The **internal company-specific list** under **47 C.F.R. §64.1200(d)** (honoured within 30
days, retained 5 years) is independent of the national registry: a number clean against the
registry is still blocked by a request made directly to the seller (`INTERNAL_DNC`).

The **established business relationship** under **16 C.F.R. §310.4(b)(1)(iii)(B)(ii)** and
**47 C.F.R. §64.1200(f)(5)** runs 18 months from a transaction and 3 months from a consumer
inquiry. It is an affirmative exemption and a narrow one — it exempts a number from the
**national registry only**. Three consequences are implemented rather than merely stated:

- It does **not** override an internal do-not-call request. Per §64.1200(d)(3) a request made
  to the seller outranks the relationship, so the internal list is evaluated first and is never
  waived.
- It does **not** reach the categorical bars or the source gates, so on `live_telephone` an EBR
  cannot move a record to eligible by itself.
- An EBR asserted **without a date is not an EBR**. The exemption is defined by the age of the
  relationship, so an undated claim cannot be shown to be inside the window and fails closed.

Windows are counted in calendar months, not 30-day approximations: 18 × 30 days falls eleven
days short of eighteen months, and that gap is the difference between an exempt call and a
violation.

The supplied fixture carries no internal-DNC or EBR columns, so both gates are inert against
it and neither appears in the exclusion table. They are exercised directly in
`tests/test_compliance.py`.

### Reassigned Numbers Database — "No Data" is not a green light

The §64.1200(m) safe harbour attaches **only to a "No" response**. This is the single most
common engineering misreading of the RND, so the three states are modelled distinctly:

| Response | Effect |
|---|---|
| `NO` | safe harbour attaches |
| `YES` | blocked — `RND_REASSIGNED` |
| `NO_DATA` | **no safe harbour** — `RND_NO_DATA_NO_SAFE_HARBOR` |

### Line type — route to the most restrictive treatment

Resolved to `wireless | landline | voip | unknown`. The distinction is legally load-bearing:
**§227(b)(1)(A)(iii)** attaches strict liability to autodialled and prerecorded calls to
wireless numbers, while landlines fall under the §227(c) DNC regime, which requires more than
one call in twelve months to be actionable.

**`voip` and `unknown` route to the wireless treatment, never the landline one.** Recorded as
a routing fact rather than a reason code — most records are wireless, so emitting a code would
flood the exclusion table without excluding anything.

### Consent provenance

For any consented record the pipeline persists the exact text presented, a hash of the disclosure, the
URL, timestamp with timezone, IP, user agent, the signature or checkbox event, every seller
named, and the full lead-source chain. **The burden of proving consent is on the caller;
unverifiable consent is functionally no consent.**

`consent.require_provenance` models this and defaults to `false` **only because the fixture
supplies consent as a bare boolean with none of that evidence**. Setting it `true` — what
production demands — takes the written channel from 25 eligible leads to **zero**. That single
line is the most honest measure available of how thin a consent flag is.

### Revocation

Append-only, immutable, honoured within **10 business days** per 47 C.F.R. §64.1200(a)(10).
The schema is built so one revocation can cascade across all campaigns for a seller: the FCC's
revoke-all scope provision has been waived twice and its current compliance date is
**31 January 2027** (DA 26-12). Building for it now is cheap; retrofitting is not.

---

## 5. Where the law is genuinely unsettled

Two areas are deliberately **not** modelled as settled, because they are not.

**One-to-one consent is dead.** The FCC's 2023 order was vacated in *Insurance Marketing
Coalition v. FCC*, 127 F.4th 303 (11th Cir. 24 Jan 2025) and formally repealed by final rule
in September 2025. `one_to_one_required: false` in the ruleset, with a comment saying why —
though many lead buyers still require it contractually, which is a commercial constraint
rather than a legal one.

**Written consent is now circuit-dependent.** *Bradford v. Sovereign Pest Control* (5th Cir.,
25 Feb 2026) held that §227(b) requires only prior express consent, which may be oral, and
declined to apply 47 C.F.R. §64.1200(a)(2)'s written requirement. The rule was not vacated and
binds everywhere outside the Fifth Circuit.

**How the design survives a change like this.** Every rule is a row or a key in versioned
configuration, every decision records the `ruleset_version` that produced it, and every
decision writes an immutable lineage row. So a circuit split becomes a new ruleset version
applied to new decisions, while old decisions remain explicable under the rules that actually
governed them. Re-litigating a past decision under today's rules would be the failure mode; it
is not possible here.

---

## 6. Data protection — what is built, and what is design

Part 5e asks for a set of controls. Being precise about which are structural and which are
prose matters more than claiming all of them.

### Built

| Control | Where |
|---|---|
| Tokenisation into a segregated vault | `src/compliance/vault.py` — name, address and raw phone in `vault_party_identity`; analytics join on an HMAC-SHA256 surrogate |
| Keyed, not merely hashed | ~10¹⁰ NANP numbers make an unkeyed hash a trivial rainbow table. An unkeyed call **raises**. |
| ZIP5 boundary per §2725(3) | ZIP5 stays analytic; everything else is vaulted. Tested. |
| Direct identifiers never in output | Asserted on every emitted record, checked against the raw fixture |
| Immutable decision lineage | `compliance_decision_lineage` — inputs digest, ruleset version, per-gate verdicts. The substrate for DPPA §2721(c)'s 5-year redisclosure records. |

The input digest **excludes direct identifiers**, so it is reproducible from the analytic
record without the vault: it proves *which values* were judged, not whose they were.

### Design-only — stated as such

**Envelope encryption and key rotation.** The token key comes from configuration; production
takes it from a KMS with documented rotation. Rotation invalidates existing tokens, so it needs
a dual-token window — designed, not built.

**Retention TTLs by record class, with deletion propagating to backups and derived tables.**
This is where most implementations quietly fail, and a stub pretending otherwise would be
worse than this paragraph.

**California Delete Act.** An accident-lead vendor meets the statutory definition of a data
broker almost exactly: a business that knowingly collects and sells personal information of
consumers with whom it has no direct relationship. Registration is due annually by 31 January;
DROP deletion-request processing has been mandatory since **1 August 2026**, at least every 45
days, with **$200/day** penalties per unprocessed request.

**Recording-consent states.** If calls are recorded: CA, FL, IL, MD, MA, MT, NV, NH, PA, WA
and others require all-party consent.

**Per-query access attribution** on any table containing direct identifiers, which DPPA
§2721(c) independently requires.

---

## Reason codes

31 codes, **every one carrying a citation** — enforced by a test, because a reason code
without a legal basis is an opinion rather than a control. The citations live in a `CITATIONS`
dict rather than docstrings: `StrEnum` silently discards a docstring written after the
assignment, which meant every decision emitted "no citation recorded" until a test caught it.

See `src/compliance/reason_codes.py`.
