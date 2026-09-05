import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/generation_packet_serial.dart';

void main() {
  test('decoder transition drains old packets and rejects stale generation work', () async {
    final GenerationPacketSerial serial = GenerationPacketSerial();
    final List<String> events = <String>[];
    final Completer<void> oldPacketRelease = Completer<void>();

    await serial.transition(1, () async => events.add('decoder-1-ready'));
    final Future<void> oldPacket = serial.processPacket(
      1,
      () async {
        events.add('packet-1-start');
        await oldPacketRelease.future;
        events.add('packet-1-end');
      },
      onStale: () async => events.add('packet-1-stale'),
    );
    final Future<void> reconnect = serial.transition(
      2,
      () async => events.add('decoder-2-ready'),
    );
    final Future<void> stalePacket = serial.processPacket(
      1,
      () async => events.add('stale-packet-decoded'),
      onStale: () async => events.add('stale-packet-dropped'),
    );

    await Future<void>.delayed(Duration.zero);
    expect(events, <String>['decoder-1-ready', 'packet-1-start']);
    oldPacketRelease.complete();
    await Future.wait(<Future<void>>[oldPacket, reconnect, stalePacket, serial.drain()]);

    expect(events, <String>[
      'decoder-1-ready',
      'packet-1-start',
      'packet-1-end',
      'decoder-2-ready',
      'stale-packet-dropped',
    ]);
    expect(serial.activeGeneration, 2);
  });

  test('failed packet work does not poison later decoder transitions', () async {
    final GenerationPacketSerial serial = GenerationPacketSerial();
    await serial.transition(1, () async {});
    await expectLater(
      serial.processPacket(
        1,
        () async => throw StateError('decode failed'),
        onStale: () async {},
      ),
      throwsStateError,
    );

    await serial.transition(2, () async {});
    expect(serial.activeGeneration, 2);
  });
}
