# News Story Selection Rubric (Generalized)

Adapted from Legolas's editorial-assessment logic. Strips out MovieWeb-specific
business rules (tags, sheet lookups, franchise names) and keeps the reusable
*shape* of the funnel: mechanical pre-filter → mandatory binary gate → tier
assignment → numeric score → mechanical enforcement. Replace the bracketed
`[PLACEHOLDERS]` with specifics for whatever site/audience/channel you're
building this for.

Default posture throughout: **skeptic**. Most candidate stories should be
skipped. A missed story is always better than a bad post — noise erodes trust
in the feed faster than a missed scoop costs you.

---

## Stage 1 — Mechanical Pre-Filter (cheap, deterministic, no LLM)

Run before anything reaches an LLM. Purely mechanical string/regex/age checks —
zero judgment calls here, just cutting obvious noise to save LLM tokens and
reduce clutter.

- **Off-topic keyword filter**: drop items matching an "always irrelevant to
  this audience" regex (e.g. topics your outlet never covers), unless a
  stronger "but this is core to us" pattern also matches (so a genuinely
  on-topic story sharing a keyword with junk isn't wrongly dropped).
- **Listicle/roundup filter**: drop titles/URLs matching listicle patterns
  ("Top 10...", "Best Netflix Shows", "Ranked:") — these tend to bridge
  unrelated stories together at clustering time and rarely represent a single
  discrete news event.
- **Stale single-source filter**: drop clusters that are still single-sourced
  after N hours (e.g. 24h) — if nobody else has picked it up by then, it's
  either not real news or already dead.

---

## Stage 2 — Mandatory Pre-Check (LLM, binary gate)

For every candidate story, before assigning ANY tier except "skip," the model
must be able to answer **YES to all** of the following. If any fail → automatic
skip, minimum relevance score.

1. **NAMED?** Does the story concern a specific, named entity (a person,
   product, title, franchise, company) — not a vague category?
   - "a new streaming show" = NO → skip
   - "[Specific Title]", "[Specific Person]" = YES

2. **EVENT?** Did a concrete, confirmed event actually happen — not
   speculation, sentiment, or momentum?
   - "taking over the internet", "fans love it", "could this mean..." = NO → skip
   - "[cancelled / confirmed / launched / broke a record]" = YES

3. **NEW?** Is this recent, breaking information — not evergreen background,
   a retrospective, or an anniversary piece?

4. **INTERESTING?** Would your actual audience click this on their phone right
   now? Not "is this technically news" — is it genuinely compelling to the
   *specific* audience you're building for?
   - Stories that pass 1–3 but usually fail this: minor personnel/logistics
     updates, a public figure's unrelated personal milestone, "beloved/iconic"
     framing with no new development, niche coverage with no existing
     audience overlap.

---

## Stage 3 — Tier Assignment (LLM, only after pre-check passes)

Define 2–4 tiers matched to your own posting policy. A workable default set:

- **`broad_trend`**: Independent coverage across N+ distinct publishers.
  Publisher tier/prestige doesn't matter here — breadth does.
  ⚠️ **Corporate-affiliation correction**: outlets owned by the same parent
  company should count as roughly half a source each when checking breadth —
  three mastheads from one media conglomerate running the same story is one
  company talking to itself, not independent corroboration. Maintain a
  parent-company map and apply this discount before counting sources.

- **`proven_topic`**: Breaking news about a subject your audience is already
  known to care about (matched against your own priority-topic list/tag
  sheet/whatever signal you track). Generic category tags alone (a platform
  name, a broad genre) should never be sufficient — the match must be a
  specific, named subject. Must also independently satisfy "a concrete event
  happened" (not an interview, retrospective, or "what to expect" piece).

- **`editorial_pick`**: Use *very sparingly*. A pure quality judgment call —
  a story that fails the mechanical tiers above but is compelling enough to
  flag anyway. Source count and prestige don't matter here; a single great
  scoop from an obscure outlet belongs here, a dull story from five trades
  does not. Requires the highest relevance-score floor of any tier. If the
  model isn't genuinely excited, it should skip — skipping an entire batch
  should be treated as an acceptable, even expected, outcome.

- **`skip`**: Everything else. When in doubt, skip.

---

## Stage 4 — Explicit Scope Boundaries

Two lists, stated explicitly rather than left implicit — models default to
being too permissive without them:

- **"[Audience] cares about"**: the concrete list of topics/categories this
  feed exists to cover.
- **"[Audience] does NOT care about"**: adjacent-but-out-of-scope categories
  people might mistakenly think belong (e.g. a related industry vertical, a
  geographically-adjacent market, a tangential format).

Then an **"ALWAYS SKIP regardless of source count"** list — patterns that
should never post no matter how much coverage they get. This is where you
encode recurring false-positive patterns as you discover them over time
(rankings/listicles, scheduling/calendar content, retrospectives, unconfirmed
rumor/speculation framing, personal-life disclosures unless directly tied to a
professional/production impact, ceremonial honors with no news hook, adjacent
verticals that share vocabulary with your core topic but aren't actually your
audience). Treat this list as a living document — every recurring bad post is
a candidate addition here.

---

## Stage 5 — Numeric Relevance Score

Only scored *after* the pre-check passes; if the pre-check failed, score is
capped low regardless of anything else.

```
10   = every reader in the audience needs this right now
8-9  = strong story (top-tier subject, major confirmed development)
6-7  = solid, on-topic story (respectable but not urgent)
4-5  = niche — do not post
1-3  = pre-check failed / wrong audience / evergreen
```

Set two thresholds in config, not in the prompt text alone (so they're
tunable without touching the LLM instructions):
- **Post floor**: minimum score to post at all, regardless of tier.
- **Editorial-pick floor**: a higher minimum specifically for the
  `editorial_pick` tier, since that tier has no mechanical corroboration to
  fall back on.

---

## Stage 6 — Mechanical Enforcement (Python/code, not LLM — runs after Stage 3–5)

Treat the LLM's tier/score as a **suggestion**, not a final decision. A
deterministic layer re-validates and can override:

- **Global floor**: any story below the post-floor score is discarded,
  regardless of what tier the LLM assigned.
- **Broad-trend validation**: re-count independent publishers with the
  parent-company discount applied; if the LLM called `broad_trend` but the
  count doesn't actually clear the bar once discounted, demote to the next
  tier down and re-apply that tier's rules.
- **Proven-topic validation**: independently check that the matched
  topic/tag is (a) on an allow-list of subjects with sufficient audience
  signal (e.g. minimum search volume/engagement history) and (b) not on a
  blocklist of overly generic tags. Fuzzy-match the LLM's stated tag against
  your ground-truth list rather than trusting free text verbatim.
  - **Upgrade path**: if the LLM under-called a story as `editorial_pick` but
    it actually has a valid, specific topic match with an already-decent
    score, upgrade it to `proven_topic` — this catches cases where the model
    was too conservative rather than too generous.
- **Editorial-pick validation**: enforce the higher score floor and a minimum
  corroboration bar (e.g. at least one credible source, or 2+ sources total)
  even for this "vibes-based" tier — quality judgment doesn't mean zero
  verification.

The reason this split matters: LLMs are good at nuanced judgment calls
(is this interesting? is this the right audience?) but inconsistent at
precise counting/threshold enforcement across a long response. Let the LLM
judge, let code count and enforce.

---

## Adapting This

1. Fill in Stage 4's two lists (cares-about / does-not-care-about /
   always-skip) with your actual audience's scope — this is 80% of the
   editorial identity of the rubric and the part most worth iterating on.
2. Pick your own tier names and thresholds for Stage 3/5 — 3–4 tiers is
   usually enough; more than that gets hard for the LLM to apply consistently.
3. If you don't have a "known priority topics" signal (search volume, tag
   sheet, etc.), you can drop the `proven_topic` tier entirely and just run
   `broad_trend` / `editorial_pick` / `skip`.
4. Feed real false positives back into the "ALWAYS SKIP" list over time —
   this list should visibly grow as the feed runs and you observe what kind
   of "technically passes but isn't actually good" story keeps slipping
   through.
5. Keep Stage 6 (mechanical enforcement) even if it feels redundant at first —
   it's what prevents slow drift where the LLM's tier-calling gradually gets
   looser over many runs with no correction.
