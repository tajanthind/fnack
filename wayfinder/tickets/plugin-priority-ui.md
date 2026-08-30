# Decide: priority-override UI — drag-to-reorder vs numeric input

wayfinder:grilling

## Question

PHASE1_BUNDLED_PLUGINS_BRIEF §4.3: add a user-facing priority override
(`priority_override` column or `PluginSetting` key) with the ordered getters
sorting by it. UI question: "A drag-to-reorder list per type (simplest correct
UX...) or a plain numeric input... ask me which you're going with before
building it, since `static/app.js` conventions should decide that."

Current frontend stack: vanilla JS (no framework) in `static/app.js` (1632
lines), Bootstrap 5.3 via CDN in the templates, fetch-based API calls, no
drag-and-drop library currently. The settings page is a single-page form with
cards + Save.

Options:
1. **Numeric input per plugin row** (type="number", min 1, step 1) — simplest,
   matches existing form conventions, zero new dependencies, deterministic
   save (writes `priority_override` values directly). Users pick numbers;
   ties fall back to manifest priority.
2. **Drag-to-reorder** — nicer UX, but requires adding a DnD dependency
   (SortableJS via CDN or hand-rolled HTML5 DnD) and a save-order endpoint
   that writes the derived priorities; more JS surface to maintain.

Recommendation: **numeric input** for Phase 1 (consistent with the current
vanilla-JS/Bootstrap form stack, no new deps); drag-reorder can be layered on
later without schema change since it just writes the same
`priority_override` values.

## Resolution

Confirmed: numeric priority input per plugin row (writes priority_override; ties fall back to manifest priority). No drag-reorder in Phase 1.

Claimed by: dev (this session). Resolved: user confirmed 2026-08-29.
