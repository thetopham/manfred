import 'package:flutter/material.dart';

import 'src/bridge_controller.dart';
import 'src/omi_ble_transport.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final BridgeController controller = BridgeController();
  await controller.initialize();
  runApp(ManfredCompanionApp(controller: controller));
}

class ManfredCompanionApp extends StatelessWidget {
  const ManfredCompanionApp({super.key, required this.controller});

  final BridgeController controller;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Manfred Companion',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff5d7c6f)),
        useMaterial3: true,
      ),
      home: CompanionHome(controller: controller),
    );
  }
}

class CompanionHome extends StatefulWidget {
  const CompanionHome({super.key, required this.controller});

  final BridgeController controller;

  @override
  State<CompanionHome> createState() => _CompanionHomeState();
}

class _CompanionHomeState extends State<CompanionHome> {
  bool _busy = false;

  Future<void> _run(Future<void> Function() action) async {
    setState(() => _busy = true);
    try {
      await action();
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(error.toString())),
        );
      }
    } finally {
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  Future<void> _editSettings() async {
    final SettingsDraft? draft = await showDialog<SettingsDraft>(
      context: context,
      builder: (BuildContext context) => SettingsDialog(
        initialEndpoint: widget.controller.config.endpoint,
        hasSavedToken: widget.controller.config.receiverToken.isNotEmpty,
        initialValidationCaptureEnabled:
            widget.controller.config.validationCaptureEnabled,
      ),
    );
    if (!mounted || draft == null) {
      return;
    }

    final String chosenToken = draft.token.trim().isEmpty
        ? widget.controller.config.receiverToken
        : draft.token.trim();
    await _run(
      () => widget.controller.saveConfig(
        endpoint: draft.endpoint,
        token: chosenToken,
        validationCaptureEnabled: draft.validationCaptureEnabled,
      ),
    );
  }

  Future<void> _scan() async {
    List<OmiScanDevice> devices = <OmiScanDevice>[];
    await _run(() async => devices = await widget.controller.scan());
    if (!mounted || devices.isEmpty) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('No Omi device found. Ensure it is awake and nearby.')),
        );
      }
      return;
    }
    final OmiScanDevice? selected = await showDialog<OmiScanDevice>(
      context: context,
      builder: (BuildContext context) => SimpleDialog(
        title: const Text('Select Omi Dev Kit 2'),
        children: devices
            .map(
              (OmiScanDevice device) => SimpleDialogOption(
                onPressed: () => Navigator.pop(context, device),
                child: ListTile(
                  title: Text(device.name),
                  subtitle: Text(
                    '${device.id} • ${device.rssi} dBm${device.likelyOmi ? ' • Omi match' : ' • manual fallback'}',
                  ),
                ),
              ),
            )
            .toList(),
      ),
    );
    if (selected != null) {
      await _run(() => widget.controller.selectDevice(selected));
    }
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: widget.controller,
      builder: (BuildContext context, Widget? child) {
        final BridgeController state = widget.controller;
        return Scaffold(
          appBar: AppBar(
            title: const Text('Manfred Companion'),
            actions: <Widget>[
              IconButton(
                onPressed: _busy || state.running ? null : _editSettings,
                icon: const Icon(Icons.settings),
              ),
            ],
          ),
          body: ListView(
            padding: const EdgeInsets.all(16),
            children: <Widget>[
              const Text(
                'Omi → S25 → Tailscale → Demerzel',
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 8),
              const Text(
                'Raw PCM is spooled on the phone before upload. Optional validation mode also retains complete Omi BLE notifications with opaque headers. Omi cloud and Funnel are not used.',
              ),
              const SizedBox(height: 16),
              _StatusTile(label: 'BLE', value: state.bleStatus),
              _StatusTile(label: 'Codec', value: state.codec?.name ?? 'unknown'),
              _StatusTile(label: 'Device', value: state.config.deviceId ?? 'not selected'),
              _StatusTile(label: 'Endpoint', value: state.config.endpoint.isEmpty ? 'not configured' : state.config.endpoint),
              _StatusTile(label: 'Packets (this capture)', value: '${state.packetsReceived}'),
              _StatusTile(label: 'Decoded PCM (this capture)', value: '${state.decodedPcmBytes} bytes'),
              _StatusTile(label: 'Uploaded (this capture)', value: '${state.uploadedChunks} chunks'),
              _StatusTile(label: 'Queued locally (all captures)', value: '${state.queuedChunks} chunks'),
              _StatusTile(
                label: 'Validation packet capture',
                value: state.config.validationCaptureEnabled ? 'enabled' : 'disabled',
              ),
              _StatusTile(
                label: 'Stale BLE packets dropped',
                value: '${state.stalePacketsDropped}',
              ),
              _StatusTile(
                label: 'Validation write errors',
                value: '${state.validationWriteErrors}',
              ),
              if (state.sourceSessionId != null)
                _StatusTile(label: 'Capture session', value: state.sourceSessionId!),
              if (state.validationEvidencePath != null)
                _StatusTile(label: 'Validation evidence', value: state.validationEvidencePath!),
              if (state.lastError != null)
                Card(
                  color: Theme.of(context).colorScheme.errorContainer,
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Text(state.lastError!),
                  ),
                ),
              const SizedBox(height: 16),
              Wrap(
                spacing: 10,
                runSpacing: 10,
                children: <Widget>[
                  OutlinedButton.icon(
                    onPressed: _busy || state.running ? null : _scan,
                    icon: const Icon(Icons.bluetooth_searching),
                    label: const Text('Find Omi'),
                  ),
                  FilledButton.icon(
                    onPressed: _busy || state.running ? null : () => _run(state.start),
                    icon: const Icon(Icons.hearing),
                    label: const Text('Start ears'),
                  ),
                  FilledButton.tonalIcon(
                    onPressed: _busy || !state.running ? null : () => _run(state.stop),
                    icon: const Icon(Icons.pause),
                    label: const Text('Stop'),
                  ),
                  OutlinedButton.icon(
                    onPressed: _busy || state.running || state.queuedChunks == 0
                        ? null
                        : () => _run(state.retryPending),
                    icon: const Icon(Icons.cloud_upload_outlined),
                    label: const Text('Upload queued audio'),
                  ),
                  TextButton.icon(
                    onPressed: _busy || state.running || state.queuedChunks == 0
                        ? null
                        : () => _run(state.deletePending),
                    icon: const Icon(Icons.delete_outline),
                    label: const Text('Delete queued audio'),
                  ),
                  TextButton.icon(
                    onPressed: _busy || state.running || state.validationEvidencePath == null
                        ? null
                        : () => _run(state.deleteValidationEvidence),
                    icon: const Icon(Icons.delete_sweep_outlined),
                    label: const Text('Delete validation packets'),
                  ),
                ],
              ),
              if (_busy) ...<Widget>[
                const SizedBox(height: 20),
                const LinearProgressIndicator(),
              ],
            ],
          ),
        );
      },
    );
  }
}

class SettingsDraft {
  const SettingsDraft({
    required this.endpoint,
    required this.token,
    required this.validationCaptureEnabled,
  });

  final String endpoint;
  final String token;
  final bool validationCaptureEnabled;
}

class SettingsDialog extends StatefulWidget {
  const SettingsDialog({
    super.key,
    required this.initialEndpoint,
    required this.hasSavedToken,
    required this.initialValidationCaptureEnabled,
  });

  final String initialEndpoint;
  final bool hasSavedToken;
  final bool initialValidationCaptureEnabled;

  @override
  State<SettingsDialog> createState() => _SettingsDialogState();
}

class _SettingsDialogState extends State<SettingsDialog> {
  late final TextEditingController _endpointController;
  late final TextEditingController _tokenController;
  late bool _validationCaptureEnabled;

  @override
  void initState() {
    super.initState();
    _endpointController = TextEditingController(text: widget.initialEndpoint);
    _tokenController = TextEditingController();
    _validationCaptureEnabled = widget.initialValidationCaptureEnabled;
  }

  @override
  void dispose() {
    _endpointController.dispose();
    _tokenController.dispose();
    super.dispose();
  }

  void _save() {
    Navigator.of(context).pop(
      SettingsDraft(
        endpoint: _endpointController.text,
        token: _tokenController.text,
        validationCaptureEnabled: _validationCaptureEnabled,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Demerzel tailnet receiver'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
          TextField(
            key: const Key('settings-endpoint'),
            controller: _endpointController,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: 'Endpoint',
              hintText: 'http://100.x.y.z:8787/audio',
            ),
          ),
          TextField(
            key: const Key('settings-token'),
            controller: _tokenController,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
              labelText: 'Receiver token',
              helperText: widget.hasSavedToken
                  ? 'Saved securely — leave blank to keep it'
                  : 'Stored in Android secure storage',
            ),
          ),
          CheckboxListTile(
            key: const Key('settings-validation-capture'),
            contentPadding: EdgeInsets.zero,
            value: _validationCaptureEnabled,
            onChanged: (bool? value) {
              setState(() => _validationCaptureEnabled = value ?? false);
            },
            title: const Text('Supervised raw-packet validation'),
            subtitle: const Text(
              'Retain complete Omi BLE notifications and decoder-generation events on this phone.',
            ),
          ),
        ],
      ),
    ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(onPressed: _save, child: const Text('Save')),
      ],
    );
  }
}

class _StatusTile extends StatelessWidget {
  const _StatusTile({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      dense: true,
      title: Text(label),
      subtitle: Text(value),
    );
  }
}
