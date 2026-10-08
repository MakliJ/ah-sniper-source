# Instructions for coding assistants

Read ../AGENTS.md and ../QWEN.md before modifying this project. They are the authority for architecture, build modes, safety and required checks. Follow the current setup and build commands in ../README.md.

- Preserve the ADMIN/USER split and Flask port 8765.
- The default build produces AuctionMonitorAdmin.exe, may embed local .env and is private.
- The --user build produces AuctionMonitor.exe without .env, cloud push or web authorization.
- Never commit real configuration, credentials, deployment domains, analytics IDs, databases, personal presets or binaries.
- Licensing reads allowlisted public_config.json or environment variables. Never hard-code a Supabase URL/key and never use the privileged admin configuration in a USER release.
- Keep auth/app/API noindex, exact auction-variant metadata, independent sniper switches, and durable account-scoped drafts.
- Run the checks required by AGENTS.md and QWEN.md. Source publication is not permission to rebuild the live ADMIN EXE or restart a tunnel.
