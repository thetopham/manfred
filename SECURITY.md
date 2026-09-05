# Private Manfred repository

Keep this repository private. Do not commit runtime databases, raw audio/images/chat observations, tokens, signing keys, device identifiers, spools, or model caches. Existing audio, operator, and vision capabilities remain separate. Deletion, archive publication locking, evidence provenance, and Android signing isolation are preserved from the current source base.

Brain consumes explicitly configured bounded exports. Manfred must not import Brain, Idle, or Fleet internals. Android signing secrets reach only apksigner, never Flutter, Gradle, or tests. Run `python scripts/validate_local.py` before publishing code. An actual APK/device acceptance is separate from Python source validation.
