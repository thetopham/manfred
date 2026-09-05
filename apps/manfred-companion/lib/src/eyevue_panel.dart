import 'dart:io';

import 'package:flutter/material.dart';

import 'eyevue_bridge.dart';
import 'eyevue_controller.dart';

class EyevuePanel extends StatefulWidget {
  const EyevuePanel({super.key, required this.controller});
  final EyevueController controller;

  @override
  State<EyevuePanel> createState() => _EyevuePanelState();
}

class _EyevuePanelState extends State<EyevuePanel> {
  String _startup = 'media';

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: widget.controller,
        builder: (BuildContext context, Widget? child) {
          final EyevueController state = widget.controller;
          final EyevueImage? image = state.latestImage;
          final String? address = state.address;
          final List<EyevueDevice> devices = <EyevueDevice>[
            ...state.devices,
            if (address != null && !state.devices.any((EyevueDevice device) => device.address == address))
              EyevueDevice(address: address, name: 'Saved EyeVue', rssi: 0),
          ];
          return Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text('EyeVue photos', style: Theme.of(context).textTheme.titleLarge),
                  const SizedBox(height: 8),
                  Text(
                    _startup == 'capture'
                        ? 'Capture and fetch reconnects glasses Wi-Fi after each new photo. '
                            'Start a session before taking pictures. Existing photos stay on the glasses. '
                            'Photos are saved on this phone; ChatGPT attachment is a separate step.'
                        : 'Experimental: capture while glasses Wi-Fi is active is still being tested. '
                            'A session watches for new photos; existing photos stay on the glasses. '
                            'Photos are saved on this phone and are not sent to ChatGPT automatically.',
                  ),
                  const SizedBox(height: 12),
                  Text(state.status),
                  if (state.ready) const Text('Ready — try the glasses shutter or Take photo.'),
                  if (address != null) Text(address, style: Theme.of(context).textTheme.bodySmall),
                  if (state.error != null)
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        Expanded(
                          child: Text(
                            state.error!,
                            style: TextStyle(color: Theme.of(context).colorScheme.error),
                          ),
                        ),
                        IconButton(
                          tooltip: 'Dismiss error',
                          onPressed: state.clearError,
                          icon: const Icon(Icons.close),
                        ),
                      ],
                    ),
                  if (devices.isNotEmpty && !state.connected && !state.connecting)
                    DropdownButton<String>(
                      isExpanded: true,
                      value: address,
                      hint: const Text('Select EyeVue'),
                      items: devices.map((EyevueDevice device) {
                        final String name = device.name.isEmpty ? 'EyeVue' : device.name;
                        return DropdownMenuItem<String>(
                          value: device.address,
                          child: Text('$name · ${device.address}', overflow: TextOverflow.ellipsis),
                        );
                      }).toList(),
                      onChanged: state.busy
                          ? null
                          : (String? value) {
                              if (value != null) {
                                state.selectDevice(value);
                              }
                            },
                    ),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: <Widget>[
                      OutlinedButton.icon(
                        onPressed: state.busy || state.connected || state.connecting ? null : state.scan,
                        icon: const Icon(Icons.bluetooth_searching),
                        label: const Text('Find EyeVue'),
                      ),
                      if (!state.connected)
                        FilledButton(
                          onPressed: state.busy || state.connecting || address == null ? null : state.connect,
                          child: const Text('Connect'),
                        ),
                      if (state.connected || state.connecting)
                        OutlinedButton(
                          onPressed: state.busy ? null : state.disconnect,
                          child: const Text('Disconnect'),
                        ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  DropdownButton<String>(
                    value: _startup,
                    isExpanded: true,
                    items: const <DropdownMenuItem<String>>[
                      DropdownMenuItem<String>(value: 'media', child: Text('Photo session')),
                      DropdownMenuItem<String>(value: 'capture', child: Text('Capture and fetch (experimental'))),
                      DropdownMenuItem<String>(value: 'live', child: Text('Alternate startup (experimental)')),
                    ],
                    onChanged: state.busy || state.sessionActive
                        ? null
                        : (String? value) {
                            if (value != null) {
                              setState(() => _startup = value);
                            }
                          },
                  ),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: <Widget>[
                      FilledButton.icon(
                        onPressed: state.canStart ? () => state.startSession(startup: _startup) : null,
                        icon: const Icon(Icons.photo_camera),
                        label: const Text('Start photo session'),
                      ),
                      OutlinedButton(
                        onPressed: state.sessionActive && !state.busy ? state.stopSession : null,
                        child: const Text('Stop photo session'),
                      ),
                      OutlinedButton(
                        onPressed: state.canCapture ? state.capture : null,
                        child: const Text('Take photo'),
                      ),
                    ],
                  ),
                  if (state.busy) const LinearProgressIndicator(),
                  if (image != null) ...<Widget>[
                    const SizedBox(height: 12),
                    Text('Latest photo: ${image.width} × ${image.height} · ${image.bytes} bytes'),
                    if (image.cachePath.isNotEmpty)
                      Image.file(
                        File(image.cachePath),
                        height: 180,
                        fit: BoxFit.contain,
                        errorBuilder: (BuildContext context, Object error, StackTrace? stackTrace) =>
                            const Text('Preview unavailable; the saved photo remains in your gallery.'),
                      ),
                  ],
                ],
              ),
            ),
          );
        },
      );
}
