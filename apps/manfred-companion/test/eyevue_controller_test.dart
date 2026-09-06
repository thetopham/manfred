import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/eyevue_bridge.dart';
import 'package:manfred_companion/src/eyevue_controller.dart';
import 'package:manfred_companion/src/eyevue_settings.dart';
import 'package:manfred_companion/src/eyevue_panel.dart';
import 'package:shared_preferences/shared_preferences.dart';

class FakeBridge implements EyevueBridge {
  final StreamController<EyevueMap> stream = StreamController<EyevueMap>.broadcast(sync: true);
  EyevueMap state = <String, Object?>{
    'type': 'state',
    'connected': true,
    'connecting': false,
    'sessionActive': false,
    'ready': false,
    'status': 'Connected',
    'androidSdkInt': 36,
    'address': 'AA:BB:CC:DD:EE:FF',
  };
  int starts = 0;
  String? lastStartup;
  int stops = 0;
  int captures = 0;
  int scans = 0;
  String? lastConnectedAddress;
  Object? startFailure;
  bool failRefreshAfterStart = false;
  bool stopCompletes = false;
  Completer<EyevueMap>? delayedState;

  @override
  Stream<EyevueMap> get events => stream.stream;
  void emitState(EyevueMap changes) {
    state = <String, Object?>{...state, ...changes};
    stream.add(state);
  }

  @override
  Future<EyevueMap> getState() async {
    if (delayedState != null) {
      return delayedState!.future;
    }
    if (starts > 0 && failRefreshAfterStart) {
      throw StateError('state reply unavailable');
    }
    return state;
  }

  @override
  Future<void> scan() async { scans++; }
  @override
  Future<void> stopScan() async {}
  @override
  Future<void> connect(String address) async {
    lastConnectedAddress = address;
    emitState(<String, Object?>{'connected': true, 'address': address});
  }
  @override
  Future<void> disconnect() async {
    emitState(<String, Object?>{'connected': false, 'sessionActive': false, 'ready': false});
  }
  @override
  Future<void> startSession(String startup) async {
    starts++;
    lastStartup = startup;
    if (startFailure != null) {
      throw startFailure!;
    }
    state = <String, Object?>{...state, 'sessionActive': true, 'ready': false};
    if (!failRefreshAfterStart) {
      stream.add(state);
    }
  }
  @override
  Future<void> stopSession() async {
    stops++;
    if (stopCompletes) {
      emitState(<String, Object?>{'sessionActive': false, 'ready': false});
    }
  }
  @override
  Future<void> capture() async { captures++; }
}

class FakeSettings implements EyevueSettings {
  String? value;
  String? source;
  Completer<String?>? pendingLoad;
  Completer<String?>? pendingSourceLoad;
  @override
  Future<String?> loadAddress() async => pendingLoad == null ? value : await pendingLoad!.future;
  @override
  Future<void> saveAddress(String address) async { value = address; }
  @override
  Future<String?> loadPhotoSource() async => pendingSourceLoad == null ? source : await pendingSourceLoad!.future;
  @override
  Future<void> savePhotoSource(String value) async { source = value; }
}

class FakePermissions implements EyevuePermissionGate {
  Object? sessionFailure;
  Completer<void>? pending;
  int? lastSdk;
  bool? lastSessionUsesWifi;
  int wifiDiscoveryRequests = 0;
  bool wifiDiscoveryGranted = false;
  bool wifiDiscoveryLocationEnabled = true;
  bool grantWifiDiscoveryOnRequest = false;
  Object? wifiDiscoveryFailure;
  @override
  Future<bool> hasWifiDiscoveryPermission() async {
    if (wifiDiscoveryFailure != null) throw wifiDiscoveryFailure!;
    return wifiDiscoveryGranted;
  }
  @override
  Future<bool> requestWifiDiscoveryPermission() async {
    wifiDiscoveryRequests++;
    if (wifiDiscoveryFailure != null) throw wifiDiscoveryFailure!;
    if (grantWifiDiscoveryOnRequest) wifiDiscoveryGranted = true;
    return wifiDiscoveryGranted;
  }
  @override
  Future<bool> isWifiDiscoveryLocationEnabled() async => wifiDiscoveryLocationEnabled;
  @override
  Future<void> requestBluetooth(int? androidSdkInt) async { lastSdk = androidSdkInt; }
  @override
  Future<void> requestSession(int? androidSdkInt, {bool usesWifi = true}) async {
    lastSdk = androidSdkInt;
    lastSessionUsesWifi = usesWifi;
    if (pending != null) {
      await pending!.future;
    }
    if (sessionFailure != null) {
      throw sessionFailure!;
    }
  }
}

Future<void> settle() => Future<void>.delayed(Duration.zero);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late FakeBridge bridge;
  late FakeSettings settings;
  late FakePermissions permissions;
  late EyevueController controller;
  late int acquired;
  late int released;

  setUp(() {
    bridge = FakeBridge();
    settings = FakeSettings();
    permissions = FakePermissions();
    acquired = 0;
    released = 0;
    controller = EyevueController(
      bridge: bridge,
      settings: settings,
      permissions: permissions,
      acquireForeground: () async { acquired++; },
      releaseForeground: () async { released++; },
    );
  });

  tearDown(() async {
    bridge.failRefreshAfterStart = false;
    bridge.delayedState = null;
    bridge.stopCompletes = true;
    controller.dispose();
    await settle();
    await bridge.stream.close();
  });


  test('saved BLE source starts without Wi-Fi permission or its battery threshold', () async {
    settings.source = 'ble_preview';
    await controller.initialize();
    bridge.emitState(<String, Object?>{
      'battery': <String, Object?>{'percent': 13, 'charging': false},
    });
    expect(controller.usesBlePreview, isTrue);
    expect(controller.canStart, isTrue);
    await controller.startSession();
    expect(bridge.lastStartup, 'ble_preview');
    expect(permissions.lastSessionUsesWifi, isFalse);
    expect(permissions.wifiDiscoveryRequests, 0);
    expect(acquired, 1);
    expect(controller.sessionActive, isTrue);
    bridge.emitState(<String, Object?>{'ready': true, 'captureSource': 'ble_preview'});
    await controller.capture();
    expect(bridge.captures, 1);
  });

  test('source changes persist and active sessions reject mode switching', () async {
    await controller.initialize();
    expect(controller.photoSource, 'wifi');
    await controller.selectPhotoSource('ble_preview');
    expect(settings.source, 'ble_preview');
    await controller.startSession();
    expect(controller.canChangePhotoSource, isFalse);
    await controller.selectPhotoSource('wifi');
    expect(settings.source, 'ble_preview');
    expect(controller.error, contains('Stop the photo session'));
    bridge.emitState(<String, Object?>{'sessionActive': false});
    await settle();
    await controller.selectPhotoSource('wifi');
    expect(controller.photoSource, 'wifi');
    expect(settings.source, 'wifi');
    await controller.startSession();
    expect(bridge.lastStartup, 'capture');
    expect(permissions.lastSessionUsesWifi, isTrue);
  });

  test('new source selection wins over a slow preference load', () async {
    settings.pendingSourceLoad = Completer<String?>();
    final Future<void> initializing = controller.initialize();
    await settle();
    await controller.selectPhotoSource('ble_preview');
    settings.pendingSourceLoad!.complete('wifi');
    await initializing;
    expect(controller.photoSource, 'ble_preview');
    expect(settings.source, 'ble_preview');
  });

  test('restored active source wins without overwriting the saved idle choice', () async {
    settings.source = 'ble_preview';
    bridge.state = <String, Object?>{
      ...bridge.state, 'sessionActive': true, 'ready': true, 'captureSource': 'wifi',
    };
    await controller.initialize();
    expect(controller.photoSource, 'wifi');
    expect(controller.canChangePhotoSource, isFalse);
    expect(settings.source, 'ble_preview');
    bridge.emitState(<String, Object?>{'sessionActive': false});
    await settle();
    expect(controller.photoSource, 'ble_preview');
  });

  test('invalid saved source preserves Wi-Fi behavior and invalid selections are rejected', () async {
    settings.source = 'unknown';
    await controller.initialize();
    expect(controller.photoSource, 'wifi');
    await controller.selectPhotoSource('unknown');
    expect(controller.error, contains('source'));
    expect(controller.photoSource, 'wifi');
    await controller.startSession();
    expect(bridge.lastStartup, 'capture');
  });

  testWidgets('source selector saves BLE and removes Wi-Fi-only controls', (WidgetTester tester) async {
    await controller.initialize();
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: SingleChildScrollView(
      child: EyevuePanel(controller: controller),
    ))));
    await tester.tap(find.byKey(const Key('eyevue-photo-source')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Instant BLE preview · 320 × 180').last);
    await tester.pumpAndSettle();
    expect(settings.source, 'ble_preview');
    expect(find.text('Take preview'), findsOneWidget);
    expect(find.text('Improve Wi-Fi discovery'), findsNothing);
    expect(find.text('Wi-Fi connection options'), findsNothing);
    bridge.emitState(<String, Object?>{
      'sessionActive': true, 'ready': true, 'captureSource': 'ble_preview',
    });
    await tester.pump();
    final DropdownButton<String> selector = tester.widget<DropdownButton<String>>(
      find.byKey(const Key('eyevue-photo-source')),
    );
    expect(selector.onChanged, isNull);
    bridge.emitState(<String, Object?>{'sessionActive': false});
    await tester.pump();
    await tester.pumpWidget(const SizedBox.shrink());
  });

  test('pushed state wins over stale initialization reply', () async {
    bridge.delayedState = Completer<EyevueMap>();
    final Future<void> initialization = controller.initialize();
    await settle();
    bridge.emitState(<String, Object?>{'connected': false, 'status': 'Disconnected'});
    bridge.delayedState!.complete(<String, Object?>{'connected': true, 'status': 'Old reply'});
    await initialization;
    expect(controller.connected, isFalse);
    expect(controller.status, 'Disconnected');
  });

  test('saved selection wins over an idle native remembered address and connects to it', () async {
    settings.value = 'device-B';
    bridge.state = <String, Object?>{...bridge.state, 'connected': false, 'address': 'device-A'};
    await controller.initialize();
    expect(controller.address, 'device-B');
    bridge.emitState(<String, Object?>{'address': 'device-A'});
    expect(controller.address, 'device-B');
    await controller.connect();
    expect(bridge.lastConnectedAddress, 'device-B');
    expect(controller.address, 'device-B');
  });

  test('active native hardware stays authoritative without losing the idle selection', () async {
    settings.value = 'device-B';
    bridge.state = <String, Object?>{...bridge.state, 'address': 'device-A'};
    await controller.initialize();
    expect(controller.connected, isTrue);
    expect(controller.address, 'device-A');
    expect(settings.value, 'device-B');
    bridge.emitState(<String, Object?>{'connected': false, 'connecting': true});
    expect(controller.address, 'device-A');
    bridge.emitState(<String, Object?>{'connecting': false, 'sessionActive': true});
    expect(controller.address, 'device-A');
    bridge.emitState(<String, Object?>{'sessionActive': false});
    expect(controller.address, 'device-B');
    await controller.connect();
    expect(bridge.lastConnectedAddress, 'device-B');
  });

  test('a new user selection wins over a slower saved-selection load', () async {
    settings.pendingLoad = Completer<String?>();
    bridge.state = <String, Object?>{...bridge.state, 'connected': false, 'address': 'device-A'};
    final Future<void> initialization = controller.initialize();
    await settle();
    await controller.selectDevice('device-B');
    settings.pendingLoad!.complete('device-A');
    await initialization;
    expect(settings.value, 'device-B');
    expect(controller.address, 'device-B');
    await controller.connect();
    expect(bridge.lastConnectedAddress, 'device-B');
  });

  test('native address events stay authoritative across delayed settings and state replies', () async {
    settings.pendingLoad = Completer<String?>();
    final Future<void> initialization = controller.initialize();
    await settle();
    bridge.emitState(<String, Object?>{'address': 'device-A'});
    expect(controller.address, 'device-A');
    bridge.delayedState = Completer<EyevueMap>();
    settings.pendingLoad!.complete('device-B');
    await settle();
    expect(controller.address, 'device-A');
    bridge.emitState(<String, Object?>{'address': 'device-C'});
    bridge.delayedState!.complete(<String, Object?>{
      'connected': false, 'sessionActive': false, 'address': 'device-A',
    });
    await initialization;
    expect(controller.connected, isTrue);
    expect(controller.address, 'device-C');
    bridge.delayedState = null;
    bridge.emitState(<String, Object?>{'connected': false});
    expect(controller.address, 'device-B');
  });

  test('stop keeps foreground ownership until native cleanup completes', () async {
    await controller.initialize();
    await controller.startSession();
    expect(acquired, 1);
    expect(controller.sessionActive, isTrue);
    expect(controller.ready, isFalse);
    await controller.stopSession();
    expect(bridge.stops, 1);
    expect(released, 0);
    expect(controller.canStart, isFalse);
    bridge.emitState(<String, Object?>{'sessionActive': false, 'ready': false});
    await settle();
    expect(released, 1);
    expect(controller.canStart, isTrue);
    bridge.emitState(<String, Object?>{'sessionActive': false});
    await settle();
    expect(released, 1);
  });

  test('capture mode retains foreground ownership while Wi-Fi reconnects', () async {
    await controller.initialize();
    await controller.startSession(startup: 'capture');
    expect(bridge.lastStartup, 'capture');
    expect(acquired, 1);
    bridge.emitState(<String, Object?>{'ready': true});
    await controller.capture();
    expect(bridge.captures, 1);
    bridge.emitState(<String, Object?>{'ready': false, 'status': 'Fetching the new photo'});
    expect(controller.sessionActive, isTrue);
    expect(controller.canCapture, isFalse);
    await controller.capture();
    expect(bridge.captures, 1);
    await settle();
    expect(released, 0);
    bridge.emitState(<String, Object?>{'ready': true, 'status': 'Ready for the next photo'});
    expect(controller.canCapture, isTrue);
    expect(acquired, 1);
    expect(released, 0);
  });

  test('optional firmware metadata and its failure stay separate from connection state', () async {
    await controller.initialize();
    bridge.emitState(<String, Object?>{
      'project': 'TK8',
      'customer': '0201',
      'firmwareStatus': 'available',
      'firmware': <String, Object?>{'btVersion': '1.2.3', 'ispVersion': '4.5.6', 'deviceVersion': '7'},
    });
    expect(controller.firmware?.btVersion, '1.2.3');
    expect(controller.firmware?.ispVersion, '4.5.6');
    expect(controller.project, 'TK8');
    bridge.emitState(<String, Object?>{
      'firmware': null,
      'firmwareStatus': 'unavailable',
      'firmwareError': 'Version query timed out',
    });
    expect(controller.firmware, isNull);
    expect(controller.connected, isTrue);
    expect(controller.error, isNull);
    expect(controller.firmwareError, 'Version query timed out');
    expect(controller.canStart, isTrue);
  });

  test('low battery prevents Wi-Fi startup until a fresh reading reaches 20 percent', () async {
    await controller.initialize();
    bridge.emitState(<String, Object?>{
      'battery': <String, Object?>{'percent': 13, 'charging': true},
    });
    expect(controller.battery?.percent, 13);
    expect(controller.battery?.charging, isTrue);
    expect(controller.canStart, isFalse);
    await controller.startSession();
    expect(acquired, 0);
    expect(bridge.starts, 0);
    expect(controller.error, contains('13%'));
    bridge.emitState(<String, Object?>{
      'battery': <String, Object?>{'percent': 20, 'charging': false},
    });
    expect(controller.canStart, isTrue);
    await controller.startSession();
    expect(bridge.starts, 1);
    expect(acquired, 1);
  });

  test('unknown or disconnected battery never becomes a stale low-power gate', () async {
    await controller.initialize();
    expect(controller.battery, isNull);
    expect(controller.canStart, isTrue);
    bridge.emitState(<String, Object?>{
      'battery': <String, Object?>{'percent': 13, 'charging': false},
    });
    expect(controller.batteryTooLowForWifi, isTrue);
    bridge.emitState(<String, Object?>{'connected': false});
    expect(controller.battery, isNull);
    bridge.emitState(<String, Object?>{'connected': true, 'battery': null});
    expect(controller.canStart, isTrue);
    for (final Object? invalid in <Object?>[
      <String, Object?>{'percent': 255, 'charging': false},
      <String, Object?>{'percent': -1, 'charging': false},
      <String, Object?>{'percent': 13.5, 'charging': false},
      <String, Object?>{'percent': 13, 'charging': 'false'},
    ]) {
      bridge.emitState(<String, Object?>{'battery': invalid});
      expect(controller.battery, isNull);
      expect(controller.canStart, isTrue);
    }
  });

  test('permission denial never acquires service or starts native capture', () async {
    await controller.initialize();
    permissions.sessionFailure = StateError('permission denied');
    await controller.startSession();
    expect(permissions.lastSdk, 36);
    expect(acquired, 0);
    expect(bridge.starts, 0);
    expect(controller.error, contains('permission denied'));
  });

  test('normal initialize scan connect and start never prompt for optional discovery', () async {
    await controller.initialize();
    await controller.scan();
    await controller.connect();
    await controller.startSession();
    expect(permissions.wifiDiscoveryRequests, 0);
    expect(controller.wifiDiscoveryPermissionGranted, isFalse);
    expect(bridge.starts, 1);
    expect(acquired, 1);
  });

  test('explicit discovery action reports permission without starting a session', () async {
    await controller.initialize();
    permissions.grantWifiDiscoveryOnRequest = true;
    await controller.improveWifiDiscovery();
    expect(permissions.wifiDiscoveryRequests, 1);
    expect(controller.wifiDiscoveryPermissionGranted, isTrue);
    expect(controller.wifiDiscoveryLocationEnabled, isTrue);
    expect(controller.wifiDiscoveryStatus, 'Discovery permission enabled');
    expect(controller.status, 'Connected');
    expect(bridge.starts, 0);
    expect(acquired, 0);
  });

  test('declined optional discovery permission keeps standard sessions available', () async {
    await controller.initialize();
    await controller.improveWifiDiscovery();
    expect(permissions.wifiDiscoveryRequests, 1);
    expect(controller.wifiDiscoveryPermissionGranted, isFalse);
    expect(controller.wifiDiscoveryStatus, contains('standard photo transfer remains available'));
    expect(controller.error, isNull);
    expect(controller.canStart, isTrue);
    await controller.startSession();
    expect(bridge.starts, 1);
    expect(permissions.wifiDiscoveryRequests, 1);
  });

  test('location services off do not block standard photo sessions', () async {
    permissions.wifiDiscoveryGranted = true;
    permissions.wifiDiscoveryLocationEnabled = false;
    await controller.initialize();
    expect(controller.wifiDiscoveryPermissionGranted, isTrue);
    expect(controller.wifiDiscoveryLocationEnabled, isFalse);
    expect(controller.wifiDiscoveryStatus, contains('Location services are off'));
    expect(controller.canStart, isTrue);
    await controller.startSession();
    expect(bridge.starts, 1);
    expect(permissions.wifiDiscoveryRequests, 0);
  });

  test('optional discovery permission errors stay separate from session errors', () async {
    permissions.wifiDiscoveryFailure = StateError('permission service unavailable');
    await controller.initialize();
    expect(controller.connected, isTrue);
    expect(controller.error, isNull);
    expect(controller.wifiDiscoveryStatus, contains('could not be checked'));
    await controller.improveWifiDiscovery();
    expect(controller.error, isNull);
    expect(controller.wifiDiscoveryStatus, contains('could not be requested'));
    await controller.startSession();
    expect(bridge.starts, 1);
    expect(controller.error, isNull);
  });

  test('concurrent start attempts share the busy boundary', () async {
    await controller.initialize();
    permissions.pending = Completer<void>();
    final Future<void> first = controller.startSession();
    await controller.startSession();
    permissions.pending!.complete();
    await first;
    expect(acquired, 1);
    expect(bridge.starts, 1);
  });

  test('native start rejection releases EyeVue lease and allows retry', () async {
    await controller.initialize();
    bridge.startFailure = StateError('another session owns EyeVue');
    await controller.startSession();
    expect(acquired, 1);
    expect(released, 1);
    expect(controller.sessionActive, isFalse);
    expect(controller.error, contains('another session'));
    bridge.startFailure = null;
    await controller.startSession();
    expect(acquired, 2);
    expect(bridge.starts, 2);
    expect(controller.sessionActive, isTrue);
  });

  test('accepted native session retains lease when state query fails', () async {
    await controller.initialize();
    bridge.failRefreshAfterStart = true;
    await controller.startSession();
    expect(controller.sessionActive, isTrue);
    expect(released, 0);
    bridge.emitState(<String, Object?>{'sessionActive': false, 'error': 'session ended'});
    await settle();
    expect(released, 1);
  });

  test('capture requires a ready active session and does not invent an image', () async {
    await controller.initialize();
    final List<EyevueImage> images = <EyevueImage>[];
    final StreamSubscription<EyevueImage> subscription = controller.images.listen(images.add);
    await controller.capture();
    expect(bridge.captures, 0);
    await controller.startSession();
    await controller.capture();
    expect(bridge.captures, 0);
    bridge.emitState(<String, Object?>{'ready': true});
    await controller.capture();
    expect(bridge.captures, 1);
    await settle();
    expect(images, isEmpty);
    bridge.emitState(<String, Object?>{'sessionActive': false, 'ready': true});
    expect(controller.ready, isFalse);
    await subscription.cancel();
  });

  test('only valid committed image events emit, even if state arrives first', () async {
    await controller.initialize();
    final List<EyevueImage> images = <EyevueImage>[];
    final StreamSubscription<EyevueImage> subscription = controller.images.listen(images.add);
    bridge.stream.add(<String, Object?>{'type': 'imageReady', 'width': 320, 'height': 180});
    await settle();
    expect(images, isEmpty);
    expect(controller.latestImage, isNull);
    final EyevueMap image = <String, Object?>{
      'type': 'imageReady',
      'id': 'photo-1',
      'sessionId': 'session-1',
      'uri': 'content://media/external/images/media/42',
      'cachePath': '/private/eyevue/photo-1.jpg',
      'fileName': 'photo-1.jpg',
      'width': 2048,
      'height': 1536,
      'bytes': 475056,
      'sha256': List<String>.filled(64, 'a').join(),
    };
    bridge.emitState(<String, Object?>{'latestImage': image});
    bridge.stream.add(image);
    bridge.stream.add(image);
    await settle();
    expect(images, hasLength(1));
    expect(controller.latestImage?.width, 2048);
    await subscription.cancel();
  });

  test('dispose during permission request never starts capture or a service', () async {
    await controller.initialize();
    permissions.pending = Completer<void>();
    final Future<void> starting = controller.startSession();
    await settle();
    controller.dispose();
    permissions.pending!.complete();
    await starting;
    await settle();
    expect(acquired, 0);
    expect(released, 0);
    expect(bridge.starts, 0);
  });

  test('native failure clears ready and releases exactly one foreground lease', () async {
    await controller.initialize();
    await controller.startSession();
    bridge.emitState(<String, Object?>{'ready': true});
    bridge.emitState(<String, Object?>{
      'sessionActive': false,
      'ready': false,
      'error': 'Wi-Fi disconnected',
    });
    await settle();
    expect(controller.ready, isFalse);
    expect(controller.error, 'Wi-Fi disconnected');
    expect(released, 1);
    controller.clearError();
    expect(controller.error, isNull);
  });

  test('EyeVue address persistence does not overwrite Omi preferences', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      'omi_device_id': 'omi-original',
      'manfred_config_v1': 'unrelated-value',
    });
    final SharedPreferencesEyevueSettings stored = SharedPreferencesEyevueSettings();
    await stored.saveAddress('eye-vue');
    await stored.savePhotoSource('ble_preview');
    expect(await stored.loadPhotoSource(), 'ble_preview');
    expect(await stored.loadAddress(), 'eye-vue');
    final SharedPreferences preferences = await SharedPreferences.getInstance();
    expect(preferences.getString('omi_device_id'), 'omi-original');
    expect(preferences.getString('manfred_config_v1'), 'unrelated-value');
  });
}
