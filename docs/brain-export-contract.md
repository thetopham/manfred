# Brain export contract

Manfred owns the companion app, Ears audio/transcription, existing vision capture, raw evidence, archive/index, deletion, and bounded scratch exports. Future Eyes through CyanBridge belong to Manfred; this extraction adds no CyanBridge functionality. No CyanBridge implementation was tracked in the extracted base.

Brain owns canonical knowledge and the consumer adapter. Neither subsystem imports the other's package or reads the other's live SQLite database.

The existing producer writes `MANFRED_STATE_DIR/wiki-inbox/YYYY-MM-DD.md`. These are local scratch inputs, not canonical Wiki raw sources. The first line is `# Wearable transcript context — YYYY-MM-DD`; the text includes `Local scratch input for daily LLM Wiki distillation` and evidence provenance. Daily dates follow `MANFRED_DAILY_TIMEZONE`, preserving the current America/Denver default.

`deploy/manfred_wiki_sync.py` transports only bounded exports. Configure `MANFRED_EXPORT_SOURCE` (or `--source`) for the producer directory, `MANFRED_BRAIN_INBOX_DIR` (or `--destination`) for the dedicated consumer mirror, and optionally `MANFRED_EXPORT_SSH_KEY`. The mirror admits canonical date-named regular UTF-8 files at most 1 MiB, validates heading/policy, keeps SHA256 entries in `wiki-sync-status.json`, and exchanges complete candidate/destination directories atomically. It requires Linux renameat2 and same-filesystem staging. An empty source fails closed by default; `--allow-empty` is an explicit operator choice.

Set Brain's `HERMES_WEARABLE_INBOX_DIR` to the same mirror directory. Its Hermes collector validates the heading/policy, bounds included characters, and treats this as external wearable scratch context. Missing exports remain missing context, not an instruction to access the archive. The five-minute mirror schedule and 23:52 final refresh are preserved as templates only.

The default source remains the established `7090:/home/matt/.hermes/manfred-ears/wiki-inbox/`; local mirror and archive defaults remain under `~/.hermes/manfred-ears`. These compatibility defaults are configurable. Repository extraction neither moves evidence nor rewrites live configuration.
