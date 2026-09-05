import 'dart:async';

import 'package:flutter_blue_plus/flutter_blue_plus.dart';

import 'omi_protocol.dart';

typedef AudioPacketCallback = void Function(OmiBlePacket packet);
typedef BleStatusCallback = void Function(String status);
typedef CodecCallback = Future<void> Function(int generation, OmiCodec codec);

class BleSetupCoordinator {
  Future<void>? _inFlight;

  Future<void> run({
    required Future<void> Function() setup,
    required Future<void> Function(Object error, StackTrace stackTrace) recover,
  }) {
    final Future<void>? current = _inFlight;
    if (current != null) {
      return current;
    }
    late final Future<void> operation;
    operation = (() async {
      try {
        await setup();
      } catch (error, stackTrace) {
        await recover(error, stackTrace);
      }
    })().whenComplete(() {
      if (identical(_inFlight, operation)) {
        _inFlight = null;
      }
    });
    _inFlight = operation;
    return operation;
  }

  Future<void> drain() => _inFlight ?? Future<void>.value();
}

class BleSetupLease {
  int _generation = 0;

  int begin() => ++_generation;

  void invalidate() {
    _generation++;
  }

  bool isCurrent(int generation) => generation == _generation;
}

class StaleBleSetup implements Exception {
  const StaleBleSetup();
}

class OmiBlePacket {
  OmiBlePacket.capture({
    required this.generation,
    required List<int> notification,
    DateTime? receivedAt,
  })  : receivedAt = (receivedAt ?? DateTime.now()).toUtc(),
        notification = List<int>.unmodifiable(notification);

  final int generation;
  final DateTime receivedAt;
  final List<int> notification;
}

class OmiScanDevice {
  const OmiScanDevice({
    required this.id,
    required this.name,
    required this.rssi,
    required this.likelyOmi,
  });

  final String id;
  final String name;
  final int rssi;
  final bool likelyOmi;
}

class OmiBleTransport {
  OmiBleTransport({
    required this.onAudioPacket,
    required this.onStatus,
    required this.onCodec,
  });

  final AudioPacketCallback onAudioPacket;
  final BleStatusCallback onStatus;
  final CodecCallback onCodec;

  BluetoothDevice? _device;
  BluetoothCharacteristic? _audioCharacteristic;
  StreamSubscription<List<int>>? _audioSubscription;
  StreamSubscription<BluetoothConnectionState>? _connectionSubscription;
  Timer? _reconnectTimer;
  bool _keepConnected = false;
  bool _linkConnected = false;
  final BleSetupCoordinator _setupCoordinator = BleSetupCoordinator();
  final BleSetupLease _setupLease = BleSetupLease();
  Future<void>? _connectInFlight;
  int _nextAudioGeneration = 1;

  Future<List<OmiScanDevice>> scan({Duration timeout = const Duration(seconds: 8)}) async {
    final List<ScanResult> results = <ScanResult>[];
    final StreamSubscription<List<ScanResult>> subscription = FlutterBluePlus.scanResults.listen(
      (List<ScanResult> update) {
        results
          ..clear()
          ..addAll(update);
      },
    );
    try {
      await FlutterBluePlus.startScan(timeout: timeout);
      await FlutterBluePlus.isScanning.where((bool scanning) => !scanning).first;
    } finally {
      await subscription.cancel();
      await FlutterBluePlus.stopScan();
    }
    final Map<String, OmiScanDevice> devices = <String, OmiScanDevice>{};
    for (final ScanResult result in results) {
      final bool serviceMatch = result.advertisementData.serviceUuids.any(
        (Guid uuid) => uuid.str.toLowerCase() == omiServiceUuid,
      );
      final String name = result.device.platformName.isNotEmpty
          ? result.device.platformName
          : result.advertisementData.advName;
      final bool nameMatch = name.toLowerCase().contains('omi');
      // Omi's own current discoverer scans without a service filter. Keep named
      // devices as a manual fallback when firmware omits the service UUID.
      if (serviceMatch || nameMatch || name.isNotEmpty) {
        devices[result.device.remoteId.str] = OmiScanDevice(
          id: result.device.remoteId.str,
          name: name.isEmpty ? 'Omi Dev Kit' : name,
          rssi: result.rssi,
          likelyOmi: serviceMatch || nameMatch,
        );
      }
    }
    final List<OmiScanDevice> sorted = devices.values.toList()
      ..sort((OmiScanDevice a, OmiScanDevice b) {
        if (a.likelyOmi != b.likelyOmi) {
          return a.likelyOmi ? -1 : 1;
        }
        return b.rssi.compareTo(a.rssi);
      });
    return sorted;
  }

  Future<void> connect(String deviceId) async {
    _keepConnected = true;
    _linkConnected = false;
    _setupLease.invalidate();
    _device = BluetoothDevice.fromId(deviceId);
    await _connectionSubscription?.cancel();
    _connectionSubscription = _device!.connectionState.listen(
      (BluetoothConnectionState state) => unawaited(_observeConnectionState(state)),
    );
    await _connectNow();
  }

  Future<void> _connectNow() {
    final Future<void>? current = _connectInFlight;
    if (current != null) {
      return current;
    }
    late final Future<void> operation;
    operation = _connectOnce().whenComplete(() {
      if (identical(_connectInFlight, operation)) {
        _connectInFlight = null;
      }
    });
    _connectInFlight = operation;
    return operation;
  }

  Future<void> _connectOnce() async {
    final BluetoothDevice? device = _device;
    if (!_keepConnected || device == null) {
      return;
    }
    try {
      onStatus('connecting');
      await device.connect(
        license: License.nonprofit,
        timeout: const Duration(seconds: 20),
        mtu: 247,
      );
      if (!_keepConnected || !identical(_device, device)) {
        return;
      }
      _linkConnected = true;
      await _ensureAudioSetup();
    } catch (_) {
      await _recoverAndReconnect('connection-error');
    }
  }

  Future<void> _observeConnectionState(BluetoothConnectionState state) async {
    try {
      await _onConnectionState(state);
    } catch (_) {
      if (_keepConnected) {
        onStatus('connection-state-error');
      }
    }
  }

  Future<void> _onConnectionState(BluetoothConnectionState state) async {
    if (state == BluetoothConnectionState.connected) {
      if (!_keepConnected) {
        return;
      }
      _linkConnected = true;
      onStatus('connected');
      await _ensureAudioSetup();
    } else if (state == BluetoothConnectionState.disconnected) {
      _linkConnected = false;
      _setupLease.invalidate();
      await _cancelAudioStream();
      onStatus(_keepConnected ? 'reconnecting' : 'disconnected');
      if (_keepConnected) {
        _scheduleReconnect();
      }
    }
  }

  Future<void> _ensureAudioSetup() async {
    while (_keepConnected && _linkConnected && _audioSubscription == null) {
      int? attemptGeneration;
      await _setupCoordinator.run(
        setup: () async {
          attemptGeneration = _setupLease.begin();
          await _setupAudio(attemptGeneration!);
        },
        recover: (Object error, StackTrace stackTrace) async {
          if (error is StaleBleSetup) {
            return;
          }
          final int? failedGeneration = attemptGeneration;
          if (failedGeneration != null) {
            await _recoverAndReconnect(
              'audio-setup-error',
              failedSetupGeneration: failedGeneration,
            );
          }
        },
      );
      if (!_keepConnected || !_linkConnected || _audioSubscription != null) {
        return;
      }
      // This caller may have joined an older setup invalidated by a reconnect.
      // Loop once the shared operation clears so the current link gets a lease.
    }
  }

  Future<void> _cancelAudioStream() async {
    final StreamSubscription<List<int>>? subscription = _audioSubscription;
    _audioSubscription = null;
    _audioCharacteristic = null;
    await subscription?.cancel();
  }

  Future<void> _recoverAndReconnect(
    String status, {
    int? failedSetupGeneration,
  }) async {
    if (!_keepConnected) {
      return;
    }
    if (failedSetupGeneration != null && !_setupLease.isCurrent(failedSetupGeneration)) {
      return;
    }
    _linkConnected = false;
    _setupLease.invalidate();
    onStatus(status);
    await _cancelAudioStream();
    try {
      await _device?.disconnect(queue: false);
    } catch (_) {
      // A failed connection or setup may already have torn down the link.
    }
    if (_keepConnected) {
      onStatus('reconnecting');
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    if (!_keepConnected || (_reconnectTimer?.isActive ?? false)) {
      return;
    }
    _reconnectTimer = Timer(
      const Duration(seconds: 3),
      () => unawaited(_connectNow()),
    );
  }

  void _requireCurrentSetup(int setupGeneration) {
    if (!_keepConnected ||
        !_linkConnected ||
        !_setupLease.isCurrent(setupGeneration)) {
      throw const StaleBleSetup();
    }
  }

  Future<void> _setupAudio(int setupGeneration) async {
    _requireCurrentSetup(setupGeneration);
    final BluetoothDevice device = _device!;
    final List<BluetoothService> services = await device.discoverServices();
    _requireCurrentSetup(setupGeneration);
    final BluetoothService service = services.firstWhere(
      (BluetoothService value) => value.uuid.str.toLowerCase() == omiServiceUuid,
    );
    final BluetoothCharacteristic codecCharacteristic = service.characteristics.firstWhere(
      (BluetoothCharacteristic value) => value.uuid.str.toLowerCase() == omiCodecUuid,
    );
    final BluetoothCharacteristic audioCharacteristic = service.characteristics.firstWhere(
      (BluetoothCharacteristic value) => value.uuid.str.toLowerCase() == omiAudioUuid,
    );
    final List<int> codecValue = await codecCharacteristic.read();
    _requireCurrentSetup(setupGeneration);
    final OmiCodec codec = OmiCodec.fromCharacteristic(codecValue);
    // Stop the old callback source before fencing queued work and replacing
    // decoder state. A late callback still carries its old generation and is
    // discarded by the controller after the transition.
    await _cancelAudioStream();
    _requireCurrentSetup(setupGeneration);
    final int generation = _nextAudioGeneration++;
    await onCodec(generation, codec);
    _requireCurrentSetup(setupGeneration);

    late final StreamSubscription<List<int>> subscription;
    subscription = audioCharacteristic.onValueReceived.listen(
      (List<int> packet) {
        if (packet.isNotEmpty) {
          onAudioPacket(
            OmiBlePacket.capture(
              generation: generation,
              notification: packet,
            ),
          );
        }
      },
      onError: (Object error) => unawaited(
        _recoverAndReconnect(
          'audio-error',
          failedSetupGeneration: setupGeneration,
        ),
      ),
    );
    _audioCharacteristic = audioCharacteristic;
    _audioSubscription = subscription;
    try {
      await audioCharacteristic.setNotifyValue(true);
      _requireCurrentSetup(setupGeneration);
    } catch (_) {
      await subscription.cancel();
      if (identical(_audioSubscription, subscription)) {
        _audioSubscription = null;
        _audioCharacteristic = null;
      }
      rethrow;
    }
    onStatus('streaming');
  }

  Future<void> disconnect() async {
    _keepConnected = false;
    _linkConnected = false;
    _setupLease.invalidate();
    _reconnectTimer?.cancel();
    final BluetoothCharacteristic? audioCharacteristic = _audioCharacteristic;
    await _cancelAudioStream();
    try {
      await audioCharacteristic?.setNotifyValue(false);
    } catch (_) {
      // The characteristic is often already gone after a BLE disconnect.
    }
    await _connectionSubscription?.cancel();
    _connectionSubscription = null;
    final BluetoothDevice? device = _device;
    try {
      await device?.disconnect(queue: false);
    } catch (_) {
      // A connect attempt may already have torn down the link.
    }
    await _setupCoordinator.drain();
    final Future<void>? connectInFlight = _connectInFlight;
    if (connectInFlight != null) {
      await connectInFlight;
    }
    _device = null;
    onStatus('disconnected');
  }
}
