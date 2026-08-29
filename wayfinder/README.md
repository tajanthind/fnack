# Wayfinder tracker (local-markdown)

This repo charts a wayfinder effort in markdown because no issue tracker is
configured.

## Map

`map.md` — the canonical low-res map: Destination, Notes, Decisions so far,
Not yet specified, Out of scope.

## Tickets

`tickets/<name>.md` — one file per decision ticket. Each carries a
`wayfinder:<type>` label line (`research` | `prototype` | `grilling` |
`task`). Blocking is written as a `Blocked by:` line in the ticket body.

A ticket is **claimed** by appending `Claimed by: <who>` to its body before
work starts. Resolving = append a `Resolution:` section, then move a one-line
gist into `map.md` → Decisions so far.

## Research findings

`research/<ticket-name>.md` — the output of resolved research tickets; the
ticket links it.

## Frontier

Open tickets (with no unresolved `Blocked by:` deps) that aren't claimed.
