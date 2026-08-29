# Zero required authentication — audit and close gaps

wayfinder:research

## Question

"We should not have a single required authentication by the end." Audit the
whole app surface and confirm/close gaps:

1. Web UI + all user-facing API routes (`/`, `/import`, `/queue`,
   `/settings`, `/api/*`): nothing should require login or a key.
2. The Lidarr/SABnzbd/Newznab/Torznab emulation endpoints currently require
   the app's `api_key` — decide and document that this is machine-to-machine
   (Lidarr's own config holds the key) and that the key is optional (generated,
   rotatable, never required for the UI). Confirm nothing about first-run
   setup forces credentials.
3. Optional services (VPN config, cookies, AcoustID key, MusicBrainz):
   verify each is strictly optional and the app works without them.
4. Flag anything that currently blocks first-run or daily use with an auth
   wall.

Deliver: an audit table (surface | auth today | required? | action) and any
fixes needed to guarantee zero required auth.

Write findings to `wayfinder/research/zero-auth-audit.md`.
