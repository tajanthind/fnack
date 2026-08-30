# Decide: extra plugin types to fold into the manifest schema now

wayfinder:grilling

## Question

HARNESS_BRIEF §3 lists plugin types/features to design into the type table +
manifest schema from the start (even if Phase 1 doesn't ship implementations):

1. **`lyrics_provider`** — sibling of `metadata_provider`, same priority-chain
   pattern (LRCLIB/Genius/Musixmatch). Add to the `type` enum now — no
   implementation needed in Phase 1. (No question — fold in.)
2. **`storage_backend`** — where finished files land (local vs S3/rclone).
   Question: should `context.fs` be backend-agnostic from day one (methods take
   logical paths, a `storage_backend` plugin resolves them) or keep concrete
   local paths (`fs.downloads_dir` etc.) and add abstraction later?
3. **`auth_provider`** — SSO/reverse-proxy-header auth, Phase 4 stretch. Add to
   the `type` enum now so it doesn't change shape twice. (No question — fold
   in, but do NOT attempt implementation.)
4. **Duplicate/quality-conflict resolution** — "same track at two bitrates,
   which do we keep" needs a decision return-value, not a pass/fail `TaskResult`.
   Question: dedicated `conflict_resolver` type, or does it fit inside
   `library_task` (TaskResult gains an optional `decision` payload)?
5. **Import/export source as downloader's mirror** — "watch a folder and import
   already-downloaded files" (this is `import_service`/`watcher_service`).
   Question: a `downloader` variant (inverse), or its own `library_source`
   type? (Cross-links with the lidarr/watcher ticket.)
6. **Bulk webhook/notification pack** — ship Discord + ntfy as two *separate*
   `event_hook` plugins (trust tiers per service), not one configurable one.
   (No question — pattern confirmed; implementations can be Phase 3/4.)

## Resolution

Confirmed: fold lyrics_provider + storage_backend + auth_provider + conflict_resolver + library_source into the manifest type enum now (no impls in Phase 1). conflict_resolver gets a dedicated interface (decision return-value); library_source is its own type; storage_backend: context.fs stays concrete local paths for Phase 1, abstraction later; Discord + ntfy as separate event_hook plugins.

Claimed by: dev (this session). Resolved: user confirmed 2026-08-29.
