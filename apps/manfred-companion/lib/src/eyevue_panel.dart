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
  String _wifiStartup = 'capture';

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: widget.controller,
        builder: (BuildContext context, Widget? child) {
          final EyevueController state = widget.controller;
          final EyevueImage? image = state.latestImage;
          final EyevueFirmware? firmware = state.firmware;
          final EyevueBattery? battery = state.battery;
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
                  DropdownButton<String>(
                    key: const Key('eyevue-photo-source'),
                    value: state.photoSource,
                    isExpanded: true,
                    items: const <DropdownMenuItem<String>>[
                      DropdownMenuItem<String>(
                        value: 'ble_preview',
                        child: Text('Instant BLE preview · 320 × 180', overflow: TextOverflow.ellipsis),
                      ),
                      DropdownMenuItem<String>(
                        value: 'wifi',
                        child: Text('Wi-Fi original · full resolution', overflow: TextOverflow.ellipsis),
                      ),
                    ],
                    onChanged: state.canChangePhotoSource
                        ? (String? value) {
                            if (value != null) state.selectPhotoSource(value);
                          }
                        : null,
                  ),
                  Text(
                    state.usesBlePreview
                        ? 'Get a small preview over Bluetooth without changing the phone’s Wi-Fi. '
                            'Start a session, then use Take preview in this app. '
                            'For the glasses shutter, choose Wi-Fi before starting the session.'
                        : 'Fetch full-resolution originals over the glasses’ Wi-Fi. '
                            'Start a session before using the glasses shutter or Take photo. '
                            'Wi-Fi connections can take around 30 seconds or fail.',
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    'Saved images can be sent to your open ChatGPT conversation by the Manfred Tasker automation.',
                  ),
                  const SizedBox(height: 12),
                  Text(state.status),
                  if (state.ready)
                    Text(state.usesBlePreview
                        ? 'Ready — tap Take preview.'
                        : 'Ready — try the glasses shutter or Take photo.'),
                  if (address != null) Text(address, style: Theme.of(context).textTheme.bodySmall),
                  if (state.project != null)
                    Text('Hardware: ${state.project} / ${state.customer ?? "unknown"}'),
                  if (firmware != null)
                    Text('Firmware: BT ${firmware.btVersion} · ISP ${firmware.ispVersion} · device ${firmware.deviceVersion}'),
                  if (battery != null)
                    Text('Glasses battery: ${battery.percent}%${battery.charging ? " · Charging" : ""}'),
                  if (state.connected && battery == null)
                    const Text('Glasses battery: unavailable'),
                  if (!state.usesBlePreview && state.batteryTooLowForWifi)
                    Text(
                      'Charge the glasses to at least 20% before Wi-Fi photo transfer.',
                      style: TextStyle(color: Theme.of(context).colorScheme.error),
                    ),
                  if (state.firmwareStatus == 'reading') const Text('Reading firmware versions…'),
                  if (state.firmwareStatus == 'unavailable')
                    Text(state.connected
                        ? 'Firmware versions unavailable; Bluetooth remains connected.'
                        : 'Firmware versions unavailable.'),
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
                  if (!state.usesBlePreview)
                    ExpansionTile(
                      tilePadding: EdgeInsets.zero,
                      title: const Text('Wi-Fi connection options'),
                      children: <Widget>[
                        DropdownButton<String>(
                          value: _wifiStartup,
                          isExpanded: true,
                          items: const <DropdownMenuItem<String>>[
                            DropdownMenuItem<String>(value: 'capture', child: Text('Capture and fetch')),
                            DropdownMenuItem<String>(value: 'media', child: Text('Keep Wi-Fi open (experimental)')),
                            DropdownMenuItem<String>(value: 'live', child: Text('Alternate startup (experimental)')),
                          ],
                          onChanged: state.canChangePhotoSource
                              ? (String? value) {
                                  if (value != null) setState(() => _wifiStartup = value);
                                }
                              : null,
                        ),
                      ],
                    ),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: <Widget>[
                      FilledButton.icon(
                        onPressed: state.canStart
                            ? () => state.startSession(startup: state.usesBlePreview ? 'ble_preview' : _wifiStartup)
                            : null,
                        icon: const Icon(Icons.photo_camera),
                        label: Text(state.usesBlePreview ? 'Start BLE preview' : 'Start photo session'),
                      ),
                      OutlinedButton(
                        onPressed: state.sessionActive && !state.busy ? state.stopSession : null,
                        child: const Text('Stop photo session'),
                      ),
                      OutlinedButton(
                        onPressed: state.canCapture ? state.capture : null,
                        child: Text(state.usesBlePreview ? 'Take preview' : 'Take photo'),
                      ),
                    ],
                  ),
                  if (!state.usesBlePreview) ...<Widget>[
                  TextButton.icon(
                    onPressed: state.busy || state.connecting ? null : state.improveWifiDiscovery,
                    icon: const Icon(Icons.wifi_find),
                    label: const Text('Improve Wi-Fi discovery'),
                  ),
                  Text(
                    'Optional: Android requires Precise location permission and Location services '
                    'to identify the glasses Wi-Fi access point. Manfred does not read GPS coordinates. '
                    'Faster connections are still being tested.',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  if (state.wifiDiscoveryStatus != null)
                    Text(state.wifiDiscoveryStatus!, style: Theme.of(context).textTheme.bodySmall),
                  ],
                  if (state.busy) const LinearProgressIndicator(),
                  if (image != null) ...<Widget>[
                    const SizedBox(height: 12),
                    Text('Latest image: ${image.width} × ${image.height} · ${image.bytes} bytes'),
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
