import 'package:flutter/services.dart';

typedef EyevueMap = Map<String, Object?>;

EyevueMap eyevueMap(Object? value) => value is Map
    ? <String, Object?>{
        for (final MapEntry<Object?, Object?> entry in value.entries)
          if (entry.key case final String key) key: entry.value,
      }
    : <String, Object?>{};

abstract interface class EyevueBridge {
  Stream<EyevueMap> get events;
  Future<EyevueMap> getState();
  Future<void> scan();
  Future<void> stopScan();
  Future<void> connect(String address);
  Future<void> disconnect();
  Future<void> startSession(String startup);
  Future<void> stopSession();
  Future<void> capture();
}

class MethodChannelEyevueBridge implements EyevueBridge {
  MethodChannelEyevueBridge({
    MethodChannel? channel,
    EventChannel? eventChannel,
  })  : channel = channel ?? const MethodChannel('manfred/eyevue'),
        eventChannel = eventChannel ?? const EventChannel('manfred/eyevue/events');

  final MethodChannel channel;
  final EventChannel eventChannel;
  late final Stream<EyevueMap> _events =
      eventChannel.receiveBroadcastStream().map(eyevueMap);

  @override
  Stream<EyevueMap> get events => _events;
  @override
  Future<EyevueMap> getState() async =>
      eyevueMap(await channel.invokeMethod<Object?>('getState'));
  @override
  Future<void> scan() => channel.invokeMethod<void>('scan');
  @override
  Future<void> stopScan() => channel.invokeMethod<void>('stopScan');
  @override
  Future<void> connect(String address) =>
      channel.invokeMethod<void>('connect', <String, Object?>{'address': address});
  @override
  Future<void> disconnect() => channel.invokeMethod<void>('disconnect');
  @override
  Future<void> startSession(String startup) => channel.invokeMethod<void>(
        'startSession',
        <String, Object?>{'startup': startup},
      );
  @override
  Future<void> stopSession() => channel.invokeMethod<void>('stopSession');
  @override
  Future<void> capture() => channel.invokeMethod<void>('capture');
}

class EyevueDevice {
  const EyevueDevice({required this.address, required this.name, required this.rssi});
  final String address;
  final String name;
  final int rssi;
}

class EyevueImage {
  const EyevueImage({
    required this.id,
    required this.sessionId,
    required this.uri,
    required this.cachePath,
    required this.fileName,
    required this.width,
    required this.height,
    required this.bytes,
    required this.sha256,
  });
  final String id;
  final String sessionId;
  final String uri;
  final String cachePath;
  final String fileName;
  final int width;
  final int height;
  final int bytes;
  final String sha256;

  static EyevueImage? tryParse(Object? value) {
    final EyevueMap map = eyevueMap(value);
    String field(String key) => map[key] is String ? map[key]! as String : '';
    int integer(String key) => map[key] is num ? (map[key]! as num).toInt() : 0;
    final String id = field('id');
    final String session = field('sessionId');
    final String uri = field('uri');
    final String path = field('cachePath');
    final String digest = field('sha256');
    final int width = integer('width');
    final int height = integer('height');
    final int bytes = integer('bytes');
    if (id.isEmpty ||
        session.isEmpty ||
        (uri.isEmpty && path.isEmpty) ||
        width <= 0 ||
        height <= 0 ||
        bytes <= 0 ||
        !RegExp(r'^[a-fA-F0-9]{64}$').hasMatch(digest)) {
      return null;
    }
    return EyevueImage(
      id: id,
      sessionId: session,
      uri: uri,
      cachePath: path,
      fileName: field('fileName'),
      width: width,
      height: height,
      bytes: bytes,
      sha256: digest,
    );
  }
}
