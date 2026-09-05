import 'dart:async';

/// Serializes ownership changes so one wearable cannot stop another's service.
class ForegroundOwners {
  ForegroundOwners({required this.startService, required this.stopService});

  final Future<void> Function() startService;
  final Future<void> Function() stopService;
  final Set<String> _owners = <String>{};
  Future<void> _tail = Future<void>.value();

  Future<void> acquire(String owner) => _serialize(() async {
        if (_owners.contains(owner)) {
          return;
        }
        if (_owners.isEmpty) {
          await startService();
        }
        _owners.add(owner);
      });

  Future<void> release(String owner) => _serialize(() async {
        if (!_owners.contains(owner)) {
          return;
        }
        if (_owners.length == 1) {
          await stopService();
        }
        _owners.remove(owner);
      });

  Future<void> _serialize(Future<void> Function() action) {
    final Future<void> operation = _tail.then((_) => action());
    _tail = operation.then<void>((_) {}, onError: (Object _, StackTrace __) {});
    return operation;
  }
}
