import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/foreground_owners.dart';

void main() {
  test('stopping ears preserves eyes and final owner stops service', () async {
    final List<String> calls = <String>[];
    final ForegroundOwners owners = ForegroundOwners(
      startService: () async { calls.add('start'); },
      stopService: () async { calls.add('stop'); },
    );
    await owners.acquire('ears');
    await owners.acquire('eyes');
    await owners.acquire('eyes');
    await owners.release('ears');
    expect(calls, <String>['start']);
    await owners.release('eyes');
    await owners.release('eyes');
    expect(calls, <String>['start', 'stop']);
  });

  test('concurrent sessions share pending startup', () async {
    final Completer<void> ready = Completer<void>();
    int starts = 0;
    int stops = 0;
    final ForegroundOwners owners = ForegroundOwners(
      startService: () { starts++; return ready.future; },
      stopService: () async { stops++; },
    );
    final Future<void> first = owners.acquire('eyes');
    final Future<void> second = owners.acquire('ears');
    final Future<void> stopped = owners.release('eyes');
    ready.complete();
    await Future.wait(<Future<void>>[first, second, stopped]);
    expect(starts, 1);
    expect(stops, 0);
    await owners.release('ears');
    expect(stops, 1);
  });

  test('failed startup does not keep phantom ownership or poison retry', () async {
    int starts = 0;
    int stops = 0;
    final ForegroundOwners owners = ForegroundOwners(
      startService: () async {
        if (++starts == 1) throw StateError('denied');
      },
      stopService: () async { stops++; },
    );
    await expectLater(owners.acquire('eyes'), throwsStateError);
    await owners.release('eyes');
    expect(stops, 0);
    await owners.acquire('ears');
    expect(starts, 2);
    await owners.release('ears');
    expect(stops, 1);
  });
}
