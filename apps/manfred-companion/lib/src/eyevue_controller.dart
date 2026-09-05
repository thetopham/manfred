import 'dart:async';

import 'package:flutter/foundation.dart';

import 'eyevue_bridge.dart';
import 'eyevue_settings.dart';

class EyevueController extends ChangeNotifier {
  EyevueController({
    EyevueBridge? bridge,
    EyevueSettings? settings,
    EyevuePermissionGate? permissions,
    required this.acquireForeground,
    required this.releaseForeground,
  })  : _bridge = bridge ?? MethodChannelEyevueBridge(),
        _settings = settings ?? SharedPreferencesEyevueSettings(),
        _permissions = permissions ?? AndroidEyevuePermissionGate();

  final EyevueBridge _bridge;
  final EyevueSettings _settings;
  final EyevuePermissionGate _permissions;
  final Future<void> Function() acquireForeground;
  final Future<void> Function() releaseForeground;
  final StreamController<EyevueImage> _images = StreamController<EyevueImage>.broadcast();
  StreamSubscription<EyevueMap>? _subscription;
  Future<void>? _releasePending;
  Completer<void>? _inactive;
  bool _disposed = false;
  bool _foregroundHeld = false;
  bool _starting = false;
  final Set<String> _emittedImageIds = <String>{};
  int _stateRevision = 0;
  int _selectionRevision = 0;
  int? _androidSdkInt;
  String? _selectedAddress;
  String? _nativeAddress;

  bool connected = false;
  bool connecting = false;
  bool sessionActive = false;
  bool ready = false;
  bool busy = false;
  String status = 'Disconnected';
  String? error;
  String? address;
  List<EyevueDevice> devices = <EyevueDevice>[];
  EyevueImage? latestImage;
  EyevueFirmware? firmware;
  EyevueBattery? battery;
  String firmwareStatus = 'not_read';
  String? firmwareError;
  String? project;
  String? customer;
  bool wifiDiscoveryPermissionGranted = false;
  bool wifiDiscoveryLocationEnabled = false;
  String? wifiDiscoveryStatus;
  Stream<EyevueImage> get images => _images.stream;
  bool get batteryTooLowForWifi => battery?.tooLowForWifi ?? false;
  bool get canStart => connected && !connecting && !busy && !sessionActive && !_foregroundHeld && !batteryTooLowForWifi;
  bool get canCapture => connected && sessionActive && ready && !busy;

  Future<void> initialize() async {
    if (_subscription != null || _disposed) {
      return;
    }
    _subscription = _bridge.events.listen(
      _onEvent,
      onError: (Object failure) {
        error = failure.toString();
        _notify();
      },
    );
    try {
      final int selectionRevision = _selectionRevision;
      final String? savedAddress = await _settings.loadAddress();
      if (selectionRevision == _selectionRevision) {
        _selectedAddress = savedAddress?.isNotEmpty == true ? savedAddress : null;
      }
      _updateAddress();
      await _refreshState();
      await _refreshWifiDiscoveryPermission();
    } catch (failure) {
      error = failure.toString();
    }
    _notify();
  }

  void _updateAddress() {
    // Native owns the identity of active hardware. While idle, a user's saved
    // selection wins over the native plugin's separately remembered address.
    address = connected || connecting || sessionActive
        ? _nativeAddress
        : _selectedAddress ?? _nativeAddress;
  }

  Future<void> _refreshState() async {
    final int revision = _stateRevision;
    final EyevueMap state = await _bridge.getState();
    // A newer pushed state wins over a slower method reply.
    if (revision == _stateRevision) {
      _applyState(state);
    }
  }

  void _onEvent(EyevueMap event) {
    if (event['type'] == 'imageReady') {
      final EyevueImage? image = EyevueImage.tryParse(event);
      if (image == null) {
        error = 'EyeVue returned incomplete image metadata.';
        _notify();
        return;
      }
      final bool isNew = _emittedImageIds.add(image.id);
      latestImage = image;
      if (isNew && !_images.isClosed) {
        _images.add(image);
      }
      _notify();
    } else if (event['type'] == 'state') {
      _applyState(event);
    }
  }

  void _applyState(EyevueMap state) {
    _stateRevision++;
    connected = state['connected'] == true;
    connecting = state['connecting'] == true;
    sessionActive = state['sessionActive'] == true;
    ready = sessionActive && state['ready'] == true;
    if (state['status'] is String) {
      status = state['status']! as String;
    }
    error = state['error'] is String ? state['error']! as String : null;
    // Never carry one device's battery reading into another connection.
    if (!connected || connecting) {
      battery = null;
    } else if (state.containsKey('battery')) {
      battery = EyevueBattery.tryParse(state['battery']);
    }
    if (state.containsKey('firmware')) {
      firmware = EyevueFirmware.tryParse(state['firmware']);
    }
    if (state['firmwareStatus'] is String) {
      firmwareStatus = state['firmwareStatus']! as String;
    }
    firmwareError = state['firmwareError'] is String ? state['firmwareError']! as String : null;
    project = state['project'] is String ? state['project']! as String : null;
    customer = state['customer'] is String ? state['customer']! as String : null;
    if (state['androidSdkInt'] is num) {
      _androidSdkInt = (state['androidSdkInt']! as num).toInt();
    }
    if (state['address'] is String && (state['address']! as String).isNotEmpty) {
      _nativeAddress = state['address']! as String;
    }
    _updateAddress();
    if (state['devices'] is List) {
      final Map<String, EyevueDevice> discovered = <String, EyevueDevice>{};
      for (final Object? value in state['devices']! as List<Object?>) {
        final EyevueMap device = eyevueMap(value);
        final Object? deviceAddress = device['address'];
        if (deviceAddress is! String || deviceAddress.isEmpty) {
          continue;
        }
        discovered[deviceAddress] = EyevueDevice(
          address: deviceAddress,
          name: device['name'] is String ? device['name']! as String : 'EyeVue',
          rssi: device['rssi'] is num ? (device['rssi']! as num).toInt() : 0,
        );
      }
      devices = discovered.values.toList(growable: false);
    }
    latestImage = EyevueImage.tryParse(state['latestImage']) ?? latestImage;
    if (!sessionActive && !_starting) {
      if (_inactive?.isCompleted == false) {
        _inactive!.complete();
      }
      unawaited(_releaseLease());
    }
    _notify();
  }

  void clearError() {
    error = null;
    _notify();
  }

  Future<void> _run(Future<void> Function() operation) async {
    if (busy || _disposed) {
      return;
    }
    busy = true;
    error = null;
    _notify();
    try {
      await operation();
    } catch (failure) {
      error = failure.toString();
    } finally {
      busy = false;
      _notify();
    }
  }

  Future<void> _refreshWifiDiscoveryPermission() async {
    try {
      wifiDiscoveryPermissionGranted = await _permissions.hasWifiDiscoveryPermission();
      wifiDiscoveryLocationEnabled = await _permissions.isWifiDiscoveryLocationEnabled();
      wifiDiscoveryStatus = !wifiDiscoveryPermissionGranted
          ? 'Discovery permission not enabled; standard photo transfer remains available.'
          : wifiDiscoveryLocationEnabled
              ? 'Discovery permission enabled'
              : 'Discovery permission enabled. Location services are off; standard photo transfer remains available.';
    } catch (_) {
      wifiDiscoveryPermissionGranted = false;
      wifiDiscoveryLocationEnabled = false;
      wifiDiscoveryStatus = 'Discovery permission could not be checked; standard photo transfer remains available.';
    }
  }

  Future<void> improveWifiDiscovery() => _run(() async {
        try {
          await _permissions.requestWifiDiscoveryPermission();
          if (!_disposed) {
            await _refreshWifiDiscoveryPermission();
          }
        } catch (_) {
          wifiDiscoveryPermissionGranted = false;
          wifiDiscoveryLocationEnabled = false;
          wifiDiscoveryStatus = 'Discovery permission could not be requested; standard photo transfer remains available.';
        }
      });

  Future<void> scan() => _run(() async {
        await _permissions.requestBluetooth(_androidSdkInt);
        if (!_disposed) {
          await _bridge.scan();
        }
      });

  Future<void> stopScan() => _run(_bridge.stopScan);

  Future<void> selectDevice(String selectedAddress) => _run(() async {
        if (connected || connecting || sessionActive) {
          throw StateError('Disconnect EyeVue before selecting another device.');
        }
        await _settings.saveAddress(selectedAddress);
        _selectionRevision++;
        _selectedAddress = selectedAddress;
        _updateAddress();
      });

  Future<void> connect() => _run(() async {
        final String? selected = address;
        if (selected == null || selected.isEmpty) {
          throw StateError('Select an EyeVue device first.');
        }
        await _permissions.requestBluetooth(_androidSdkInt);
        if (_disposed) {
          return;
        }
        await _bridge.stopScan();
        await _bridge.connect(selected);
        await _refreshState();
      });

  Future<void> disconnect() => _run(() async {
        await _bridge.disconnect();
        await _refreshState();
      });

  Future<void> startSession({String startup = 'capture'}) => _run(() async {
        if (!connected || sessionActive || _foregroundHeld) {
          throw StateError('Connect EyeVue and stop the previous photo session first.');
        }
        if (batteryTooLowForWifi) {
          throw StateError('Glasses battery is ${battery!.percent}%. Charge to at least 20% before starting Wi-Fi photo transfer.');
        }
        if (startup != 'media' && startup != 'live' && startup != 'capture') {
          throw ArgumentError.value(startup, 'startup');
        }
        _starting = true;
        try {
          await _releasePending;
          await _permissions.requestSession(_androidSdkInt);
          if (_disposed) {
            return;
          }
          await acquireForeground();
          _foregroundHeld = true;
          if (_disposed) {
            return;
          }
          final int revision = _stateRevision;
          await _bridge.startSession(startup);
          // An accepted session remains owned even if the following state query fails.
          if (revision == _stateRevision) {
            sessionActive = true;
            ready = false;
            status = 'Starting photo session';
          }
          await _refreshState();
        } catch (_) {
          // Synchronous rejection creates no native session.
          if (!sessionActive) {
            await _releaseLease();
          }
          rethrow;
        } finally {
          _starting = false;
          if (!sessionActive) {
            if (_inactive?.isCompleted == false) {
              _inactive!.complete();
            }
            await _releaseLease();
          }
        }
      });

  Future<void> stopSession() => _run(() async {
        await _bridge.stopSession();
        // Native stays active until transport cleanup; its terminal event releases the lease.
        await _refreshState();
      });

  Future<void> capture() => _run(() async {
        if (!connected || !sessionActive || !ready) {
          throw StateError('Wait until the photo session is ready.');
        }
        await _bridge.capture();
      });

  Future<void> _releaseLease() {
    if (_releasePending != null) {
      return _releasePending!;
    }
    if (!_foregroundHeld) {
      return Future<void>.value();
    }
    _foregroundHeld = false;
    final Future<void> release = Future<void>.sync(releaseForeground).catchError(
      (Object failure) {
        error = 'Could not release the EyeVue foreground session: $failure';
        _notify();
      },
    );
    _releasePending = release.whenComplete(() {
      _releasePending = null;
      _notify();
    });
    return _releasePending!;
  }

  void _notify() {
    if (!_disposed) {
      notifyListeners();
    }
  }

  Future<void> _close() async {
    try {
      if (sessionActive || _starting) {
        _inactive = Completer<void>();
        await _bridge.stopSession();
        await _refreshState();
        if (sessionActive || _starting) {
          await _inactive!.future;
        }
      }
      await _releaseLease();
    } catch (_) {
      // Native Activity disposal also cancels its session. Keep cleanup errors out of UI teardown.
      await _releaseLease();
    } finally {
      await _subscription?.cancel();
      await _images.close();
    }
  }

  @override
  void dispose() {
    if (_disposed) {
      return;
    }
    _disposed = true;
    unawaited(_close());
    super.dispose();
  }
}
