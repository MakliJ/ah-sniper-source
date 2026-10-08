# Project Rules

Read `QWEN.md` before changing this repository. It is the source of truth for architecture, build modes, release boundaries, and production safety.

- Preserve the admin/user EXE split. Never ship `supabase_config.json`, `.env`, tunnel tokens, or admin binaries in a user release.
- Keep Flask on port `8765` and Cloudflare Tunnel startup idempotent through `start_all.bat`.
- Run `python tests/test_build_gates.py`, `python -m py_compile desktop/main.py desktop/ahgem.py`, and `git diff --check` before committing.
- Treat `example.invalid` as the public web surface: only the landing page and explicitly documented public data routes may be indexable; app/auth/API routes must remain `noindex`.
- Analytics IDs are configuration, never source data. Do not commit credentials or hard-code measurement IDs.
- For live deployment, rebuild the admin EXE and restart the tunnel only after explicit user approval; source changes and git push are not a deployment.
- BoE price, stats, sockets, and tertiary effects must belong to the same auction variant. Decode current lot data; never infer stats from item ID, ilvl, price, or historical manual mappings. Unknown metadata must remain unknown.
- Keep sniper, browser, and item-card filtering consistent, including the price shown after filtering. Preserve independent BoE subfilters and the separate tertiary-stat selector.
- `Snipe items` and `BoE Snipe` are independent switches. The main sniper block requires `items_enabled` and an explicit matching item ID; an empty ID list must never select all items, regardless of discount, average, or search. Persist the switch without clearing filters; default it to enabled for older presets. Keep the browser catalog independent of this sniper-only rule.
- Preset edits must survive reloads and cloud outages. Keep drafts scoped to the account/local EXE and preset ID; remove them only after a confirmed save. Never replace a failed cloud read with empty defaults or let an older response erase newer edits.
- On Supabase recovery, retry with small probes and publish a complete snapshot. BoE variants and presets use existing JSON fields; do not require a schema migration for the current implementation.
- For BoE or preset changes, also run `python tests/test_boe_variants.py`, `node tests/test_boe_web.js`, `python tests/test_preset_persistence.py`, and `node tests/test_preset_store.js`. Use temporary presets for UI checks and verify the user's original presets are unchanged.
