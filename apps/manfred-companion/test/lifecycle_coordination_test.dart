import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/lifecycle_coordination.dart';

void main() {
  test('stop finalization does not wait for a blocked network drain', () async {
    final BridgeStopCoordinator coordinator = BridgeStopCoordinator();
    final Completer<void> blockedUpload = Completer<void>();
    final List<String> events = <String>[];

    await coordinator
        .stop(
          disconnect: () async => events.add('disconnect'),
          drainPackets: () async => events.add('packets-drained'),
          markNotRunning: () => events.add('not-running'),
          commitFinalChunk: () async => events.add('final-chunk-durable'),
          scheduleUpload: () {
            events.add('upload-scheduled');
            return blockedUpload.future;
          },
          closeValidation: () async => events.add('validation-closed'),
          disposeCapture: () => events.add('capture-disposed'),
          stopForeground: () async => events.add('foreground-stopped'),
          refreshQueued: () async => events.add('queue-refreshed'),
          markStopped: () => events.add('stopped'),
        )
        .timeout(const Duration(milliseconds: 100));

    expect(blockedUpload.isCompleted, isFalse);
    expect(events, <String>[
      'disconnect',
      'packets-drained',
      'not-running',
      'final-chunk-durable',
      'upload-scheduled',
      'validation-closed',
      'capture-disposed',
      'foreground-stopped',
      'queue-refreshed',
      'stopped',
    ]);
  });

  test('background drain schedules a fresh tail without blocking its caller', () async {
    final BackgroundDrainCoordinator coordinator = BackgroundDrainCoordinator();
    final Completer<void> firstRelease = Completer<void>();
    final Completer<void> secondStarted = Completer<void>();
    int passes = 0;

    Future<void> pass() async {
      passes++;
      if (passes == 1) {
        await firstRelease.future;
      } else if (!secondStarted.isCompleted) {
        secondStarted.complete();
      }
    }

    coordinator.scheduleFresh(pass);
    expect(passes, 1);
    expect(coordinator.inFlight, isNotNull);

    firstRelease.complete();
    await secondStarted.future.timeout(const Duration(milliseconds: 100));
    await coordinator.whenSettled();
    expect(passes, 2);
  });

  test('settlement includes a fresh tail scheduled after the active pass', () async {
    final BackgroundDrainCoordinator coordinator = BackgroundDrainCoordinator();
    final Completer<void> firstRelease = Completer<void>();
    final Completer<void> tailRelease = Completer<void>();
    int passes = 0;

    Future<void> pass() async {
      passes++;
      await (passes == 1 ? firstRelease.future : tailRelease.future);
    }

    coordinator.scheduleFresh(pass);
    final Future<void> settled = coordinator.whenSettled();
    firstRelease.complete();
    await Future<void>.delayed(Duration.zero);
    expect(passes, 2);

    bool completed = false;
    unawaited(settled.then((_) => completed = true));
    await Future<void>.delayed(Duration.zero);
    expect(completed, isFalse);

    tailRelease.complete();
    await settled.timeout(const Duration(milliseconds: 100));
  });

  test('pending count reconciliation cannot erase concurrent enqueue evidence', () {
    final PendingCount count = PendingCount(12);
    final int staleRefresh = count.beginReconciliation();

    count.recordEnqueue();
    expect(count.value, 13);
    expect(count.reconcile(staleRefresh, 12), isFalse);
    expect(count.value, 13);

    final int currentRefresh = count.beginReconciliation();
    expect(count.reconcile(currentRefresh, 13), isTrue);
    count.recordRemoval();
    expect(count.value, 12);
  });

  test('replacing upload ownership invalidates the old drain pass', () {
    final UploadLease lease = UploadLease();
    final int oldUploader = lease.current();
    expect(lease.isCurrent(oldUploader), isTrue);

    lease.invalidate();
    expect(lease.isCurrent(oldUploader), isFalse);
    expect(lease.isCurrent(lease.current()), isTrue);
  });
}
