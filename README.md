# Manfred

Current implementation, installed-build evidence and unfinished work: [September5/6 Manfred handoff](docs/handoff-2026-09-06.md).

Manfred is the app and its services: the wearable Companion client, Ears audio/transcription, and existing visual evidence capture. The companion now includes an experimental EyeVue photo session; see [EyeVue photos](docs/eyevue-photos.md) for setup and [the E09-family hardware profile](docs/hardware/eyevue-e09.md) for the tested TK8/0201 device, firmware interfaces, and measured capabilities. It activates no backend workers or services.

## Ownership

- `apps/manfred-companion/`: Android/Flutter app, BLE audio bridge, durable spool, uploads, controls, and tests.
- `manfred_ears/`: audio receiver, archive, VAD/ASR, search, retention/deletion, image-centered episodes, and existing vision receiver.
- `deploy/`: configurable unit templates and bounded Brain export transfer.
- `scripts/`: independent source validation, Android validation, and runtime planning/install/check.
- `tests/`: server, export, configuration, installation, and Android helper regressions.
- `docs/historical/`: labeled preserved prior validation evidence.

Brain owns canonical knowledge, its API, and consumption of the explicit wearable export contract. Idle owns task selection/execution/review. Fleet owns hardware control. Manfred imports none of those private packages. Read [the export contract](docs/brain-export-contract.md), [Ears implementation guide](docs/ears.md), and [source provenance](LICENSE_PROVENANCE.md).

## Local validation and package

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-validate.txt
.venv/bin/python scripts/validate_local.py
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/manfred --help
```

Full ASR runtime dependencies are in `requirements.txt`; optional Parakeet dependencies remain separate in `requirements-parakeet.txt`. Python tests use synthetic inputs and do not require GPUs or live archives. Android changes additionally require `scripts/validate_android_local.py` with Flutter/Android tools. Device installation and physical capture acceptance remain separate; retain the stable signing identity and require `--require-stable-signing` for an installable update. Signing material is not included here.

Android CI is owned by this repository's [Manfred Android workflow](.github/workflows/android.yml). Pull requests run validation without signing secrets. Main, the explicit EyeVue testing branch, and manual runs produce a stable-signed APK with its source commit and SHA-256 receipt. The signing identity is preserved from the earlier app; no recurring Idle or Fleet work is part of this workflow.

## Runtime plan and later cutover

```sh
MANFRED_RUNTIME_ROOT="$HOME/.local/share/manfred" python scripts/install_runtime.py plan --role manfred-data-plane
python scripts/install_runtime.py plan --role demerzel
```

Review the plan before a later authorized `apply`. `apply` copies only owned runtime source, creates a local venv/launcher, and renders selected user units; dependency installation is opt-in through `--install-runtime-deps`. It never rewrites the secret environment file, copies evidence, migrates a database, restarts/enables services, or changes scheduling. `check` compares installed source, rendered units, and CLI availability; requesting dependency installation/check additionally verifies service imports. It does not claim live service health.

`MANFRED_RUNTIME_ROOT` defaults to `~/.local/share/manfred`, with `manfred_ears/` and `.venv/` below it and `~/.local/bin/manfred` as launcher. The archive default `~/.hermes/manfred-ears`, environment file `~/.config/manfred-ears/manfred-ears.env`, existing ports, and source/mirror defaults are retained for compatibility. Configure runtime, state, environment path, receiver hosts, forward addresses, export source, and Brain inbox through installer arguments/environment. Unit templates use explicit placeholders and must be rendered; do not copy them directly. Existing secret-file `MANFRED_STATE_DIR` remains authoritative; pass the matching state directory for unit write access.

A future cutover must review the actual service account, host mappings, state path and permissions, installed dependency backend, rendered unit diff, and rollback runtime before daemon-reload/restart. Source separation itself does not warrant an archive transfer. The Brain mirror directory must be dedicated, writable, and on a filesystem supporting atomic directory exchange.

Chat Mirror is retained as a separate unmerged candidate by the extraction owner. Its branch predates newer archive/deletion/deployment fixes and must be reconciled before adoption. Current source provenance is Brain commit `595671676933d13a3065f88e1f3e25b858ace556`.
