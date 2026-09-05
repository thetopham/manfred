import 'dart:async';

/// Owns one drain pass at a time and can queue one fresh tail pass without
/// making a capture lifecycle await network I/O.
class BackgroundDrainCoordinator {
  Future<void>? _inFlight;

  Future<void>? get inFlight => _inFlight;

  Future<void> run(Future<void> Function() pass) {
    final Future<void>? current = _inFlight;
    if (current != null) {
      return current;
    }
    late final Future<void> operation;
    operation = pass().whenComplete(() {
      if (identical(_inFlight, operation)) {
        _inFlight = null;
      }
    });
    _inFlight = operation;
    return operation;
  }

  void scheduleFresh(Future<void> Function() pass) {
    final Future<void> first = run(pass);
    unawaited(
      first
          .then<void>((_) {}, onError: (Object _, StackTrace __) {})
          .then((_) => run(pass))
          .then<void>((_) {}, onError: (Object _, StackTrace __) {}),
    );
  }

  Future<void> whenIdle() => _inFlight ?? Future<void>.value();

  Future<void> whenSettled() async {
    while (true) {
      final Future<void>? current = _inFlight;
      if (current != null) {
        try {
          await current;
        } catch (_) {
          // The owner records pass failures; settlement only waits for release.
        }
      }
      // scheduleFresh installs its tail from a completion callback. Yield once
      // so that callback either publishes the next pass or finishes.
      await Future<void>.delayed(Duration.zero);
      if (_inFlight == null) {
        return;
      }
    }
  }
}

/// Tracks the visible queue without allowing an expensive stale directory scan
/// to overwrite evidence enqueued or removed while that scan was in flight.
class PendingCount {
  PendingCount([this.value = 0]);

  int value;
  int _revision = 0;

  int beginReconciliation() => _revision;

  bool reconcile(int revision, int actual) {
    if (revision != _revision) {
      return false;
    }
    value = actual;
    return true;
  }

  void recordEnqueue() {
    _revision++;
    value++;
  }

  void recordRemoval() {
    _revision++;
    if (value > 0) {
      value--;
    }
  }

  void replace(int actual) {
    _revision++;
    value = actual;
  }
}

/// Invalidates a drain pass when endpoint or credential ownership changes.
class UploadLease {
  int _generation = 0;

  int current() => _generation;

  void invalidate() {
    _generation++;
  }

  bool isCurrent(int generation) => generation == _generation;
}

/// Keeps Stop an evidence-finalization boundary, not a network-drain boundary.
class BridgeStopCoordinator {
  Future<void> stop({
    required Future<void> Function() disconnect,
    required Future<void> Function() drainPackets,
    required void Function() markNotRunning,
    required Future<void> Function() commitFinalChunk,
    required Future<void> Function() scheduleUpload,
    required Future<void> Function() closeValidation,
    required void Function() disposeCapture,
    required Future<void> Function() stopForeground,
    required Future<void> Function() refreshQueued,
    required void Function() markStopped,
  }) async {
    await disconnect();
    await drainPackets();
    markNotRunning();
    await commitFinalChunk();
    unawaited(scheduleUpload());
    await closeValidation();
    disposeCapture();
    await stopForeground();
    await refreshQueued();
    markStopped();
  }
}
