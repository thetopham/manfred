import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

const String chatGptAndroidPackage = 'com.openai.chatgpt';
const String chatMirrorCaptureMethod = 'android-accessibility';

class ChatMirrorObservation {
  ChatMirrorObservation._({
    required this.payload,
    required this.mirrorSessionId,
    required this.sequenceNumber,
    required this.observedAt,
    required this.sourcePackage,
    required this.eventKind,
    required this.captureMethod,
    required this.finality,
    required this.completeness,
    required this.observedText,
  });

  static const Set<String> _allowedFields = <String>{
    'schema_version',
    'mirror_session_id',
    'sequence_number',
    'observed_at',
    'clock_basis',
    'source_package',
    'event_kind',
    'capture_method',
    'finality',
    'completeness',
    'observed_text',
    'observable_conversation_id',
    'interruptions',
    'image_refs',
    'audio_refs',
    'automation_events',
    'gaps',
    'metadata',
  };
  static const Set<String> _eventKinds = <String>{
    'session_started',
    'ui_snapshot',
    'session_ended',
    'gap',
    'automation',
  };

  final Uint8List payload;
  final String mirrorSessionId;
  final int sequenceNumber;
  final DateTime observedAt;
  final String sourcePackage;
  final String eventKind;
  final String captureMethod;
  final String finality;
  final String completeness;
  final String observedText;

  static ChatMirrorObservation decode(Uint8List payload) {
    if (payload.isEmpty || payload.length > 262144) {
      throw const FormatException('Chat Mirror observation size is invalid');
    }
    final Object? decoded;
    try {
      decoded = jsonDecode(utf8.decode(payload, allowMalformed: false));
    } on Object catch (error) {
      throw FormatException('Chat Mirror observation is not UTF-8 JSON', error);
    }
    if (decoded is! Map<String, dynamic>) {
      throw const FormatException('Chat Mirror observation must be a JSON object');
    }
    final Set<String> unexpected = decoded.keys.toSet().difference(_allowedFields);
    if (unexpected.isNotEmpty) {
      throw FormatException('Chat Mirror observation has unexpected field: ${unexpected.first}');
    }
    final Set<String> missing = _allowedFields
        .difference(<String>{'observable_conversation_id'})
        .difference(decoded.keys.toSet());
    if (missing.isNotEmpty) {
      throw FormatException('Chat Mirror observation is missing field: ${missing.first}');
    }
    if (decoded['schema_version'] != 1) {
      throw const FormatException('Unsupported Chat Mirror schema version');
    }
    final String sessionId = _identifier(decoded['mirror_session_id'], 'mirror_session_id');
    if (!RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$').hasMatch(sessionId)) {
      throw const FormatException('Chat Mirror session identifier is invalid');
    }
    final Object? rawSequence = decoded['sequence_number'];
    if (rawSequence is! int || rawSequence < 0) {
      throw const FormatException('Chat Mirror sequence number is invalid');
    }
    final DateTime observedAt = _timestamp(decoded['observed_at']);
    if (decoded['clock_basis'] != 'android-system-clock') {
      throw const FormatException('Unsupported Chat Mirror clock basis');
    }
    if (decoded['source_package'] != chatGptAndroidPackage) {
      throw const FormatException('Unsupported Chat Mirror source package');
    }
    final Object? rawEventKind = decoded['event_kind'];
    if (rawEventKind is! String || !_eventKinds.contains(rawEventKind)) {
      throw const FormatException('Unsupported Chat Mirror event kind');
    }
    final String eventKind = rawEventKind;
    if (decoded['capture_method'] != chatMirrorCaptureMethod) {
      throw const FormatException('Unsupported Chat Mirror capture method');
    }
    final Object? rawFinality = decoded['finality'];
    if (rawFinality is! String ||
        (rawFinality != 'provisional' && rawFinality != 'final')) {
      throw const FormatException('Chat Mirror finality is invalid');
    }
    final String finality = rawFinality;
    final Object? rawCompleteness = decoded['completeness'];
    if (rawCompleteness is! String ||
        !<String>{'partial', 'complete', 'gap'}.contains(rawCompleteness)) {
      throw const FormatException('Chat Mirror completeness is invalid');
    }
    final String completeness = rawCompleteness;
    final Object? rawText = decoded['observed_text'];
    if (rawText is! String || utf8.encode(rawText).length > 100000) {
      throw const FormatException('Chat Mirror observed text is invalid');
    }
    final Object? conversationId = decoded['observable_conversation_id'];
    if (conversationId != null) {
      _identifier(conversationId, 'observable_conversation_id');
    }
    for (final String key in <String>[
      'interruptions',
      'image_refs',
      'audio_refs',
      'automation_events',
      'gaps',
    ]) {
      final Object? value = decoded[key];
      if (value is! List<Object?> || value.length > 100) {
        throw FormatException('Chat Mirror $key must be a bounded list');
      }
    }
    final Object? metadata = decoded['metadata'];
    if (metadata is! Map<String, dynamic> || metadata.length > 64) {
      throw const FormatException('Chat Mirror metadata must be a bounded object');
    }
    return ChatMirrorObservation._(
      payload: payload,
      mirrorSessionId: sessionId,
      sequenceNumber: rawSequence,
      observedAt: observedAt,
      sourcePackage: chatGptAndroidPackage,
      eventKind: eventKind,
      captureMethod: chatMirrorCaptureMethod,
      finality: finality,
      completeness: completeness,
      observedText: rawText,
    );
  }

  static String _identifier(Object? value, String label) {
    if (value is! String ||
        value.isEmpty ||
        value.length > 128 ||
        value.codeUnits.any((int unit) => unit < 32)) {
      throw FormatException('Chat Mirror $label is invalid');
    }
    return value;
  }

  static DateTime _timestamp(Object? value) {
    if (value is! String || value.length > 64) {
      throw const FormatException('Chat Mirror observed_at is invalid');
    }
    final DateTime parsed;
    try {
      parsed = DateTime.parse(value);
    } on FormatException {
      throw const FormatException('Chat Mirror observed_at must be ISO8601');
    }
    if (!value.endsWith('Z') && !RegExp(r'[+-]\d\d:\d\d$').hasMatch(value)) {
      throw const FormatException('Chat Mirror observed_at must include a timezone');
    }
    return parsed.toUtc();
  }
}

class PendingChatMirrorEvent {
  const PendingChatMirrorEvent({
    required this.id,
    required this.mirrorSessionId,
    required this.sequenceNumber,
    required this.observedAt,
    required this.idempotencyKey,
    required this.payloadSha256,
    required this.payloadPath,
  });

  final String id;
  final String mirrorSessionId;
  final int sequenceNumber;
  final DateTime observedAt;
  final String idempotencyKey;
  final String payloadSha256;
  final String payloadPath;

  Map<String, Object> toJson() => <String, Object>{
        'id': id,
        'mirror_session_id': mirrorSessionId,
        'sequence_number': sequenceNumber,
        'observed_at': observedAt.toUtc().toIso8601String(),
        'idempotency_key': idempotencyKey,
        'payload_sha256': payloadSha256,
        'payload_path': payloadPath,
      };

  factory PendingChatMirrorEvent.fromJson(Map<String, Object?> json) {
    return PendingChatMirrorEvent(
      id: json['id']! as String,
      mirrorSessionId: json['mirror_session_id']! as String,
      sequenceNumber: json['sequence_number']! as int,
      observedAt: DateTime.parse(json['observed_at']! as String).toUtc(),
      idempotencyKey: json['idempotency_key']! as String,
      payloadSha256: json['payload_sha256']! as String,
      payloadPath: json['payload_path']! as String,
    );
  }
}

class ChatMirrorSpool {
  ChatMirrorSpool(this.root);

  final Directory root;
  Future<void>? _initializationFuture;

  void _validatePending(PendingChatMirrorEvent pending) {
    if (!RegExp(r'^[0-9a-f]{32}$').hasMatch(pending.id) ||
        !RegExp(r'^[0-9a-f]{64}$').hasMatch(pending.payloadSha256) ||
        pending.idempotencyKey.isEmpty ||
        pending.idempotencyKey.length > 512 ||
        pending.idempotencyKey.codeUnits.any((int unit) => unit < 32) ||
        pending.payloadPath != '${root.path}/${pending.id}.payload.json') {
      throw const FormatException('Chat Mirror spool metadata is invalid');
    }
  }

  Future<void> initialize() => _initializationFuture ??= _initializeOnce();

  Future<void> _initializeOnce() async {
    try {
      await root.create(recursive: true);
      await _reconcileInterruptedEnqueues();
    } catch (_) {
      _initializationFuture = null;
      rethrow;
    }
  }

  Future<void> _reconcileInterruptedEnqueues() async {
    await for (final FileSystemEntity entity in root.list()) {
      if (entity is! File || !entity.path.endsWith('.meta.json.tmp')) {
        continue;
      }
      final File committedMetadata = File(entity.path.substring(0, entity.path.length - 4));
      if (await committedMetadata.exists()) {
        await entity.delete();
        continue;
      }
      try {
        final Object? decoded = jsonDecode(await entity.readAsString());
        if (decoded is! Map<String, Object?>) {
          continue;
        }
        final PendingChatMirrorEvent pending = PendingChatMirrorEvent.fromJson(decoded);
        _validatePending(pending);
        final String expectedPayloadPath = '${root.path}/${pending.id}.payload.json';
        if (pending.payloadPath != expectedPayloadPath) {
          continue;
        }
        final File committedPayload = File(expectedPayloadPath);
        final File temporaryPayload = File('$expectedPayloadPath.tmp');
        final File? candidate = await committedPayload.exists()
            ? committedPayload
            : (await temporaryPayload.exists() ? temporaryPayload : null);
        if (candidate == null) {
          continue;
        }
        final String currentHash = sha256.convert(await candidate.readAsBytes()).toString();
        if (currentHash != pending.payloadSha256) {
          continue;
        }
        if (candidate.path == temporaryPayload.path) {
          await temporaryPayload.rename(committedPayload.path);
        }
        await entity.rename(committedMetadata.path);
      } on FormatException {
        // Preserve malformed evidence for explicit inspection.
      } on TypeError {
        // Preserve malformed evidence for explicit inspection.
      }
    }
  }

  Future<PendingChatMirrorEvent> enqueue(Uint8List payload) async {
    await initialize();
    final ChatMirrorObservation observation = ChatMirrorObservation.decode(payload);
    final String payloadHash = sha256.convert(payload).toString();
    final String idempotencyKey =
        '${observation.mirrorSessionId}:${observation.sequenceNumber}:$payloadHash';
    final String id = sha256.convert(utf8.encode(idempotencyKey)).toString().substring(0, 32);
    final File payloadFile = File('${root.path}/$id.payload.json');
    final File metadataFile = File('${root.path}/$id.meta.json');
    final PendingChatMirrorEvent pending = PendingChatMirrorEvent(
      id: id,
      mirrorSessionId: observation.mirrorSessionId,
      sequenceNumber: observation.sequenceNumber,
      observedAt: observation.observedAt,
      idempotencyKey: idempotencyKey,
      payloadSha256: payloadHash,
      payloadPath: payloadFile.path,
    );

    if (await metadataFile.exists()) {
      final Object? existingDecoded = jsonDecode(await metadataFile.readAsString());
      if (existingDecoded is! Map<String, Object?>) {
        throw StateError('Committed Chat Mirror metadata is invalid');
      }
      final PendingChatMirrorEvent existing = PendingChatMirrorEvent.fromJson(existingDecoded);
      _validatePending(existing);
      if (existing.idempotencyKey != pending.idempotencyKey ||
          existing.payloadSha256 != pending.payloadSha256 ||
          !await payloadFile.exists() ||
          sha256.convert(await payloadFile.readAsBytes()).toString() != pending.payloadSha256) {
        throw StateError('Committed Chat Mirror evidence conflicts with the new observation');
      }
      return existing;
    }

    final File payloadTemporary = File('${payloadFile.path}.tmp');
    final File metadataTemporary = File('${metadataFile.path}.tmp');
    await metadataTemporary.writeAsString(jsonEncode(pending.toJson()), flush: true);
    await payloadTemporary.writeAsBytes(payload, flush: true);
    await payloadTemporary.rename(payloadFile.path);
    await metadataTemporary.rename(metadataFile.path);
    return pending;
  }

  Future<List<PendingChatMirrorEvent>> pending() async {
    await initialize();
    final List<PendingChatMirrorEvent> events = <PendingChatMirrorEvent>[];
    await for (final FileSystemEntity entity in root.list()) {
      if (entity is! File || !entity.path.endsWith('.meta.json')) {
        continue;
      }
      try {
        final Object? decoded = jsonDecode(await entity.readAsString());
        if (decoded is! Map<String, Object?>) {
          continue;
        }
        final PendingChatMirrorEvent pending = PendingChatMirrorEvent.fromJson(decoded);
        _validatePending(pending);
        if (await File(pending.payloadPath).exists()) {
          events.add(pending);
        }
      } on FormatException {
        continue;
      } on TypeError {
        continue;
      } on StateError {
        continue;
      } on FileSystemException {
        continue;
      }
    }
    events.sort((PendingChatMirrorEvent a, PendingChatMirrorEvent b) {
      final int bySession = a.mirrorSessionId.compareTo(b.mirrorSessionId);
      return bySession != 0 ? bySession : a.sequenceNumber.compareTo(b.sequenceNumber);
    });
    return events;
  }

  Future<void> remove(PendingChatMirrorEvent pending) async {
    _validatePending(pending);
    for (final String path in <String>[
      pending.payloadPath,
      '${root.path}/${pending.id}.meta.json',
    ]) {
      final File file = File(path);
      if (await file.exists()) {
        await file.delete();
      }
    }
  }

  Future<void> deleteAll() async {
    await initialize();
    if (await root.exists()) {
      await root.delete(recursive: true);
    }
    _initializationFuture = null;
    await initialize();
  }
}

class ChatMirrorEndpoint {
  const ChatMirrorEndpoint(this.uri);

  final Uri uri;

  factory ChatMirrorEndpoint.parse(String value) {
    final Uri uri = Uri.parse(value.trim());
    if (uri.path != '/chat-mirror' || uri.fragment.isNotEmpty || uri.hasQuery) {
      throw const FormatException('Chat Mirror endpoint must be exactly /chat-mirror');
    }
    final bool tailnetIp = _isTailscaleIpv4(uri.host);
    final bool tailnetDns = uri.host.toLowerCase().endsWith('.ts.net');
    if (!((uri.scheme == 'http' && tailnetIp) ||
        (uri.scheme == 'https' && (tailnetIp || tailnetDns)))) {
      throw const FormatException('Chat Mirror endpoint must be a tailnet address');
    }
    return ChatMirrorEndpoint(uri);
  }

  static bool _isTailscaleIpv4(String host) {
    final List<String> parts = host.split('.');
    if (parts.length != 4) {
      return false;
    }
    final List<int> octets = parts.map(int.tryParse).whereType<int>().toList();
    return octets.length == 4 &&
        octets.every((int value) => value >= 0 && value <= 255) &&
        octets[0] == 100 &&
        octets[1] >= 64 &&
        octets[1] <= 127;
  }
}

class ChatMirrorLocalIntegrityException implements Exception {
  const ChatMirrorLocalIntegrityException(this.message);

  final String message;

  @override
  String toString() => message;
}

class ChatMirrorUploadException implements Exception {
  const ChatMirrorUploadException({required this.statusCode, required this.uri});

  final int statusCode;
  final Uri uri;

  bool get isPermanentEventFailure =>
      const <int>{400, 409, 413, 415, 422}.contains(statusCode);

  @override
  String toString() => 'Manfred Chat Mirror rejected event with HTTP $statusCode';
}

class ChatMirrorUploader {
  ChatMirrorUploader({
    required this.endpoint,
    required this.receiverToken,
    http.Client? client,
    this.requestTimeout = const Duration(seconds: 10),
  }) : client = client ?? http.Client();

  final ChatMirrorEndpoint endpoint;
  final String receiverToken;
  final http.Client client;
  final Duration requestTimeout;

  Future<void> upload(PendingChatMirrorEvent pending) async {
    final Uint8List payload;
    try {
      payload = await File(pending.payloadPath).readAsBytes();
    } on FileSystemException catch (error) {
      throw ChatMirrorLocalIntegrityException(
        'Committed Chat Mirror payload cannot be read: ${error.message}',
      );
    }
    final String currentHash = sha256.convert(payload).toString();
    if (currentHash != pending.payloadSha256) {
      throw const ChatMirrorLocalIntegrityException(
        'Committed Chat Mirror payload no longer matches its SHA-256 metadata',
      );
    }
    final http.Response response = await client.post(
      endpoint.uri,
      headers: <String, String>{
        HttpHeaders.contentTypeHeader: 'application/json',
        'X-Manfred-Chat-Token': receiverToken,
        'Idempotency-Key': pending.idempotencyKey,
        'X-Manfred-Content-SHA256': currentHash,
      },
      body: payload,
    ).timeout(requestTimeout);
    if (response.statusCode != HttpStatus.accepted) {
      throw ChatMirrorUploadException(
        statusCode: response.statusCode,
        uri: endpoint.uri,
      );
    }
  }

  void close() => client.close();
}

class ChatMirrorDrainResult {
  const ChatMirrorDrainResult({
    required this.uploaded,
    required this.newlyRejected,
    required this.retainedRejected,
  });

  final int uploaded;
  final int newlyRejected;
  final int retainedRejected;
}

Future<ChatMirrorDrainResult> drainChatMirrorSpool({
  required ChatMirrorSpool spool,
  required ChatMirrorUploader uploader,
  required Set<String> rejectedEventIds,
  bool Function()? shouldStop,
}) async {
  int uploaded = 0;
  int newlyRejected = 0;
  for (final PendingChatMirrorEvent pending in await spool.pending()) {
    if (shouldStop?.call() ?? false) {
      break;
    }
    if (rejectedEventIds.contains(pending.id)) {
      continue;
    }
    try {
      await uploader.upload(pending);
    } on ChatMirrorLocalIntegrityException {
      rejectedEventIds.add(pending.id);
      newlyRejected++;
      continue;
    } on ChatMirrorUploadException catch (error) {
      if (!error.isPermanentEventFailure) {
        rethrow;
      }
      rejectedEventIds.add(pending.id);
      newlyRejected++;
      continue;
    }
    await spool.remove(pending);
    rejectedEventIds.remove(pending.id);
    uploaded++;
  }
  return ChatMirrorDrainResult(
    uploaded: uploaded,
    newlyRejected: newlyRejected,
    retainedRejected: rejectedEventIds.length,
  );
}

class NativeChatMirrorRecord {
  const NativeChatMirrorRecord({required this.name, required this.payload});

  final String name;
  final Uint8List payload;
}

abstract interface class NativeChatMirrorBridge {
  Future<bool> isEnabled();
  Future<void> openAccessibilitySettings();
  Future<List<NativeChatMirrorRecord>> listPending({String? afterName});
  Future<List<String>> listPendingNames();
  Future<bool> deletePending(String name, {String? expectedSha256});
  Future<void> fsyncSpoolDirectory(String path);
}

class MethodChannelNativeChatMirrorBridge implements NativeChatMirrorBridge {
  MethodChannelNativeChatMirrorBridge({
    MethodChannel? channel,
  }) : channel = channel ?? const MethodChannel('com.thetopham.manfred_companion/chat_mirror');

  final MethodChannel channel;
  static final RegExp _safeName = RegExp(r'^[A-Za-z0-9_-]+\.json$');

  @override
  Future<bool> isEnabled() async => await channel.invokeMethod<bool>('isEnabled') ?? false;

  @override
  Future<void> openAccessibilitySettings() => channel.invokeMethod<void>('openAccessibilitySettings');


  String _safePendingName(Object? item) {
    if (item is! Map<Object?, Object?>) {
      throw const FormatException('Native Chat Mirror record is invalid');
    }
    final Object? nameValue = item['name'];
    if (nameValue is! String ||
        !_safeName.hasMatch(nameValue) ||
        nameValue.contains('..')) {
      throw const FormatException('Native Chat Mirror record is unsafe');
    }
    return nameValue;
  }

  @override
  Future<List<NativeChatMirrorRecord>> listPending({String? afterName}) async {
    if (afterName != null &&
        (!_safeName.hasMatch(afterName) || afterName.contains('..'))) {
      throw const FormatException('Native Chat Mirror cursor is unsafe');
    }
    final List<Object?> raw =
        await channel.invokeMethod<List<Object?>>(
              'listPending',
              <String, String?>{'afterName': afterName},
            ) ??
            <Object?>[];
    final List<NativeChatMirrorRecord> records = <NativeChatMirrorRecord>[];
    for (final Object? item in raw) {
      final String name = _safePendingName(item);
      final Object? bodyValue = (item as Map<Object?, Object?>)['body'];
      final Uint8List? payload = bodyValue is Uint8List
          ? Uint8List.fromList(bodyValue)
          : bodyValue is List<int>
              ? Uint8List.fromList(bodyValue)
              : null;
      if (payload == null) {
        continue;
      }
      records.add(
        NativeChatMirrorRecord(
          name: name,
          payload: payload,
        ),
      );
    }
    return records;
  }

  @override
  Future<List<String>> listPendingNames() async {
    final List<Object?> raw =
        await channel.invokeMethod<List<Object?>>('listPendingNames') ?? <Object?>[];
    return raw.map((Object? item) {
      if (item is! String || !_safeName.hasMatch(item) || item.contains('..')) {
        throw const FormatException('Native Chat Mirror filename is unsafe');
      }
      return item;
    }).toList(growable: false);
  }

  @override
  Future<bool> deletePending(String name, {String? expectedSha256}) async {
    if (!_safeName.hasMatch(name) || name.contains('..')) {
      throw const FormatException('Native Chat Mirror filename is unsafe');
    }
    if (expectedSha256 != null && !RegExp(r'^[0-9a-f]{64}$').hasMatch(expectedSha256)) {
      throw const FormatException('Native Chat Mirror expected SHA-256 is invalid');
    }
    return await channel.invokeMethod<bool>(
          'deletePending',
          <String, String?>{
            'name': name,
            'expectedSha256': expectedSha256,
          },
        ) ??
        false;
  }

  @override
  Future<void> fsyncSpoolDirectory(String path) {
    if (path.isEmpty || path.codeUnits.any((int unit) => unit < 32)) {
      throw const FormatException('Chat Mirror spool directory is invalid');
    }
    return channel.invokeMethod<void>(
      'fsyncSpoolDirectory',
      <String, String>{'path': path},
    );
  }
}

class ChatMirrorImportCoordinator {
  ChatMirrorImportCoordinator({required this.nativeBridge, required this.spool});

  final NativeChatMirrorBridge nativeBridge;
  final ChatMirrorSpool spool;

  Future<int> importOnce() async {
    int imported = 0;
    String? afterName;
    while (true) {
      final List<NativeChatMirrorRecord> records =
          await nativeBridge.listPending(afterName: afterName);
      if (records.isEmpty) {
        return imported;
      }
      for (final NativeChatMirrorRecord record in records) {
        try {
          await spool.enqueue(record.payload);
        } on FormatException {
          continue;
        }
        await nativeBridge.fsyncSpoolDirectory(spool.root.path);
        await nativeBridge.deletePending(
          record.name,
          expectedSha256: sha256.convert(record.payload).toString(),
        );
        imported++;
      }
      afterName = records.last.name;
    }
  }

  Future<int> deleteNativePending() async {
    int deleted = 0;
    while (true) {
      final List<String> names = await nativeBridge.listPendingNames();
      if (names.isEmpty) {
        return deleted;
      }
      for (final String name in names) {
        await nativeBridge.deletePending(name);
        deleted++;
      }
    }
  }
}
