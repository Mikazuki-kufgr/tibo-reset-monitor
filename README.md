# Quiet Codex reset monitor

Public announcement discovery and author-verified classification for Codex and ChatGPT Work global quota resets. No X account or OpenAI API credentials required. Only explicit upcoming global resets and official completion statements notify; banked reset notices are off by default.

Python 3.11+, standard library only. Run `python -m unittest discover -s tests -v` and `python -m monitor replay` for isolated local validation. Production discovery uses RSS/API candidates followed by FxEmbed original-post and reply context checks. Predictions, ordinary product posts, negatives and uncertain claims stay quiet.

GitHub Actions runs every five minutes when repository variable `MONITOR_ENABLED=true`. First manual run sets `bootstrap=true`; later runs must preserve the `monitor-state` branch. Set only `NTFY_TOPIC` in Repository Secrets for an anonymous private random ntfy channel; protected channels can use `NTFY_TOKEN` after adjusting config. Never put real topic values or tokens in this repository.

The state branch contains public announcement IDs, event classifications, freshness/error categories and per-channel delivery states. It never contains the phone topic, account credentials, local Windows service identity, or QQ records. Sending markers are committed before delivery; interrupted or ambiguous delivery is quarantined instead of blindly repeated. Standard public-repository runners are free under GitHub's current policy, but schedules may be delayed or dropped.

Discovery data: https://codex-reset.com/ and https://reset.onlyax.com/. Verification: https://docs.fxembed.com/. No affiliation with OpenAI, X or these community services. These sources have no end-to-end delivery SLA and cannot prove every account's quota changed. Link to the original author post accompanies real notices. Do not treat announcement publication time as an inferred deadline.

Source code is MIT licensed. Third-party short excerpts in test fixtures retain their original rights and source links.
