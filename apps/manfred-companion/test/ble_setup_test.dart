import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/omi_ble_transport.dart';

void main() {
  test('connected-state setup callers share failure recovery and can retry', () async {
    final BleSetupCoordinator coordinator = BleSetupCoordinator();
    final Completer<void> firstAttempt = Completer<void>();
    int setupAttempts = 0;
    int recoveries = 0;

    Future<void> setup() async {
      setupAttempts++;
      if (setupAttempts == 1) {
        await firstAttempt.future;
      }
    }

    Future<void> recover(Object error, StackTrace stackTrace) async {
      recoveries++;
      expect(error, isA<StateError>());
    }

    final Future<void> fromConnect = coordinator.run(setup: setup, recover: recover);
    final Future<void> fromConnectedState = coordinator.run(setup: setup, recover: recover);
    expect(setupAttempts, 1);
    expect(coordinator.drain(), same(fromConnect));

    firstAttempt.completeError(StateError('service discovery failed'));
    await Future.wait(<Future<void>>[fromConnect, fromConnectedState]);
    expect(recoveries, 1);

    await coordinator.run(setup: setup, recover: recover);
    expect(setupAttempts, 2);
    expect(recoveries, 1);
  });

  test('setup leases invalidate every in-flight await boundary', () {
    final BleSetupLease lease = BleSetupLease();
    final int first = lease.begin();
    expect(lease.isCurrent(first), isTrue);

    lease.invalidate();
    expect(lease.isCurrent(first), isFalse);

    final int reconnect = lease.begin();
    expect(reconnect, greaterThan(first));
    expect(lease.isCurrent(reconnect), isTrue);
  });

  test('BLE packet captures generation, ingress time, and defensive bytes', () {
    final List<int> source = <int>[1, 2, 3, 4];
    final DateTime ingress = DateTime.parse('2026-08-22T18:00:00-06:00');
    final OmiBlePacket packet = OmiBlePacket.capture(
      generation: 7,
      notification: source,
      receivedAt: ingress,
    );
    source[3] = 99;

    expect(packet.generation, 7);
    expect(packet.receivedAt, ingress.toUtc());
    expect(packet.notification, <int>[1, 2, 3, 4]);
    expect(() => packet.notification[0] = 99, throwsUnsupportedError);
  });
}
