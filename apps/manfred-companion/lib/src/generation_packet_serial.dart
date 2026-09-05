import 'dart:async';

/// Serializes decoder transitions and packet work while fencing reconnects.
///
/// Operations submitted before a transition finish against the old decoder.
/// Packets tagged with an old generation after a transition are rejected rather
/// than being decoded against fresh Opus state.
class GenerationPacketSerial {
  Future<void> _tail = Future<void>.value();
  int? _activeGeneration;

  int? get activeGeneration => _activeGeneration;

  Future<void> transition(
    int generation,
    FutureOr<void> Function() replaceDecoder,
  ) {
    return _enqueue(() async {
      final int? active = _activeGeneration;
      if (active != null && generation <= active) {
        throw StateError(
          'Decoder generations must increase monotonically: active=$active requested=$generation',
        );
      }
      await replaceDecoder();
      _activeGeneration = generation;
    });
  }

  Future<void> processPacket(
    int generation,
    FutureOr<void> Function() process, {
    required FutureOr<void> Function() onStale,
  }) {
    return _enqueue(() async {
      if (_activeGeneration != generation) {
        await onStale();
        return;
      }
      await process();
    });
  }

  Future<void> drain() => _tail;

  Future<void> _enqueue(FutureOr<void> Function() operation) {
    final Completer<void> result = Completer<void>();
    _tail = _tail.then((_) async {
      try {
        await operation();
        result.complete();
      } catch (error, stackTrace) {
        result.completeError(error, stackTrace);
      }
    });
    return result.future;
  }
}
