# Build instructions

Use ../README.md for setup, dependencies and commands; read ../QWEN.md and ../AGENTS.md before any change.

`python build_exe.py` builds the private ADMIN executable, AuctionMonitorAdmin.exe. It can embed local .env values and must never be distributed.

`python build_exe.py --user` builds dist/AuctionMonitor.exe with USER gates. It excludes .env, cloud push and web authorization. PyArmor trial limitations must be reported accurately.

`python tools/prepare_release.py` prepares only the USER folder. It requires valid public_config.json or equivalent public environment settings and exports only supabase_url and anon_key. It rejects privileged keys and does not copy the admin configuration, tokens or owner settings.

Keep deployment addresses, contacts, analytics IDs and keys in local ignored configuration. No hard-coded licensing settings or placeholder .env should be shipped to users.

Run the documented build gates, configuration/release/source-safety checks and relevant BoE/preset/auth tests before committing. A Git push is not a live deployment; rebuilding the running ADMIN EXE or restarting the tunnel requires separate explicit owner approval.
