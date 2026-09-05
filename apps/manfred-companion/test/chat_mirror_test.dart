import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:manfred_companion/src/chat_mirror.dart';

Uint8List observationBytes({
  String sessionId = 'chat-session-1',
  int sequence = 0,
  String text = 'User: explain the episode contract\nAssistant: Raw evidence remains canonical.',
}) {
  return Uint8List.fromList(
    utf8.encode(
      jsonEncode(<String, Object?>{
        'schema_version': 1,
        'mirror_session_id': sessionId,
        'sequence_number': sequence,
        'observed_at': '2026-08-25T20:00:00.000Z',
        'clock_basis': 'android-system-clock',
        'source_package': 'com.openai.chatgpt',
        'event_kind': 'ui_snapshot',
        'capture_method': 'android-accessibility',
        'finality': 'provisional',
        'completeness': 'partial',
        'observed_text': text,
        'observable_conversation_id': null,
        'interruptions': <Object?>[],
        'image_refs': <Object?>[],
        'audio_refs': <Object?>[],
        'automation_events': <Object?>[],
        'gaps': <Object?>[],
        'metadata': <String, Object?>{'event_type': 2048, 'window_id': 7},
      }),
    ),
  );
}

class FakeNativeChatMirrorBridge implements NativeChatMirrorBridge {
  FakeNativeChatMirrorBridge(
    this.records, {
    this.nameBatchLimit,
    this.bodyBatchLimit,
  });

  final List<NativeChatMirrorRecord> records;
  final int? nameBatchLimit;
  final int? bodyBatchLimit;
  final List<String> deleted = <String>[];
  final List<String> fsyncedPaths = <String>[];
  final List<String> operations = <String>[];

  @override
  Future<bool> isEnabled() async => true;

  @override
  Future<void> openAccessibilitySettings() async {}

  @override
  Future<List<NativeChatMirrorRecord>> listPending({String? afterName}) async {
    final List<NativeChatMirrorRecord> page = records
        .where((NativeChatMirrorRecord record) =>
            afterName == null || record.name.compareTo(afterName) > 0)
        .toList(growable: false);
    page.sort((NativeChatMirrorRecord a, NativeChatMirrorRecord b) => a.name.compareTo(b.name));
    return bodyBatchLimit == null
        ? page
        : page.take(bodyBatchLimit!).toList(growable: false);
  }

  @override
  Future<List<String>> listPendingNames() async {
    final Iterable<NativeChatMirrorRecord> batch = nameBatchLimit == null
        ? records
        : records.take(nameBatchLimit!);
    return batch.map((NativeChatMirrorRecord record) => record.name).toList(growable: false);
  }

  @override
  Future<bool> deletePending(String name, {String? expectedSha256}) async {
    NativeChatMirrorRecord? current;
    for (final NativeChatMirrorRecord record in records) {
      if (record.name == name) {
        current = record;
        break;
      }
    }
    if (expectedSha256 != null &&
        current != null &&
        sha256.convert(current.payload).toString() != expectedSha256) {
      return false;
    }
    deleted.add(name);
    operations.add('delete:$name');
    records.removeWhere((NativeChatMirrorRecord record) => record.name == name);
    return true;
  }

  @override
  Future<void> fsyncSpoolDirectory(String path) async {
    fsyncedPaths.add(path);
    operations.add('fsync:$path');
  }
}

class ReplacingNativeChatMirrorBridge extends FakeNativeChatMirrorBridge {
  ReplacingNativeChatMirrorBridge(
    super.records, {
    required this.replacementPayload,
  });

  final Uint8List replacementPayload;
  bool replaced = false;

  @override
  Future<bool> deletePending(String name, {String? expectedSha256}) async {
    if (!replaced && expectedSha256 != null) {
      final int index = records.indexWhere((NativeChatMirrorRecord record) => record.name == name);
      if (index >= 0) {
        records[index] = NativeChatMirrorRecord(name: name, payload: replacementPayload);
        replaced = true;
      }
    }
    return super.deletePending(name, expectedSha256: expectedSha256);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('observation validates bounded ChatGPT-only accessibility schema', () {
    final ChatMirrorObservation parsed = ChatMirrorObservation.decode(observationBytes());
    expect(parsed.mirrorSessionId, 'chat-session-1');
    expect(parsed.sequenceNumber, 0);
    expect(parsed.sourcePackage, 'com.openai.chatgpt');
    expect(parsed.captureMethod, 'android-accessibility');
    expect(parsed.completeness, 'partial');
    expect(parsed.observedText, contains('Raw evidence remains canonical'));

    final Map<String, Object?> wrongPackage =
        jsonDecode(utf8.decode(observationBytes())) as Map<String, Object?>;
    wrongPackage['source_package'] = 'com.example.other';
    expect(
      () => ChatMirrorObservation.decode(Uint8List.fromList(utf8.encode(jsonEncode(wrongPackage)))),
      throwsFormatException,
    );

    final Map<String, Object?> unexpected =
        jsonDecode(utf8.decode(observationBytes())) as Map<String, Object?>;
    unexpected['unexpected'] = true;
    expect(
      () => ChatMirrorObservation.decode(Uint8List.fromList(utf8.encode(jsonEncode(unexpected)))),
      throwsFormatException,
    );
  });

  test('observation enforces its text limit in UTF-8 bytes', () {
    final String accepted = List<String>.filled(30000, '界').join();
    expect(
      ChatMirrorObservation.decode(observationBytes(text: accepted)).observedText,
      accepted,
    );
    final String rejected = List<String>.filled(40000, '界').join();
    expect(
      () => ChatMirrorObservation.decode(observationBytes(text: rejected)),
      throwsFormatException,
    );
  });

  test('endpoint policy permits only exact tailnet chat-mirror receiver', () {
    expect(
      ChatMirrorEndpoint.parse('http://100.64.0.10:8790/chat-mirror').uri.path,
      '/chat-mirror',
    );
    expect(
      ChatMirrorEndpoint.parse('https://manfred.tailnet.ts.net/chat-mirror').uri.scheme,
      'https',
    );
    expect(
      () => ChatMirrorEndpoint.parse('http://192.168.1.2:8790/chat-mirror'),
      throwsFormatException,
    );
    expect(
      () => ChatMirrorEndpoint.parse('http://100.64.0.10:8790/audio'),
      throwsFormatException,
    );
    expect(
      () => ChatMirrorEndpoint.parse('http://100.64.0.10:8790/chat-mirror?token=leak'),
      throwsFormatException,
    );
  });

  test('spool commits payload before metadata and survives restart', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-spool-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final Uint8List payload = observationBytes();
    final PendingChatMirrorEvent pending = await spool.enqueue(payload);

    expect(await File(pending.payloadPath).readAsBytes(), payload);
    expect(pending.payloadSha256, sha256.convert(payload).toString());
    expect(pending.idempotencyKey, contains('chat-session-1:0:'));
    expect(await spool.pending(), hasLength(1));

    final ChatMirrorSpool restarted = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final List<PendingChatMirrorEvent> recovered = await restarted.pending();
    expect(recovered, hasLength(1));
    expect(recovered.single.id, pending.id);
  });

  test('malformed spool metadata does not block valid queued evidence', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-spool-malformed-');
    addTearDown(() => temp.delete(recursive: true));
    final Directory root = Directory('${temp.path}/spool');
    final ChatMirrorSpool spool = ChatMirrorSpool(root);
    final PendingChatMirrorEvent valid = await spool.enqueue(observationBytes());
    final File malformed = File('${root.path}/broken.meta.json');
    await malformed.writeAsString('{broken', flush: true);

    final List<PendingChatMirrorEvent> pending = await spool.pending();
    expect(pending.map((PendingChatMirrorEvent event) => event.id), <String>[valid.id]);
    expect(await malformed.exists(), isTrue);
  });

  test('spool quarantines metadata that points outside its evidence root', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-spool-path-');
    addTearDown(() => temp.delete(recursive: true));
    final Directory root = Directory('${temp.path}/spool');
    await root.create(recursive: true);
    final Uint8List payload = observationBytes();
    final File outside = File('${temp.path}/outside.json');
    await outside.writeAsBytes(payload, flush: true);
    final String id = List<String>.filled(32, 'a').join();
    await File('${root.path}/$id.meta.json').writeAsString(
      jsonEncode(<String, Object?>{
        'id': id,
        'mirror_session_id': 'chat-session-1',
        'sequence_number': 0,
        'observed_at': '2026-08-25T20:00:00.000Z',
        'idempotency_key': 'chat-session-1:0:${sha256.convert(payload)}',
        'payload_sha256': sha256.convert(payload).toString(),
        'payload_path': outside.path,
      }),
      flush: true,
    );
    final ChatMirrorSpool spool = ChatMirrorSpool(root);
    expect(await spool.pending(), isEmpty);
    expect(await File('${root.path}/$id.meta.json').exists(), isTrue);
    expect(await outside.readAsBytes(), payload);
  });

  test('native observations are deleted only after durable spool commit', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-import-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final FakeNativeChatMirrorBridge native = FakeNativeChatMirrorBridge(<NativeChatMirrorRecord>[
      NativeChatMirrorRecord(name: 'first.json', payload: observationBytes()),
    ]);
    final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
      nativeBridge: native,
      spool: spool,
    );

    expect(await importer.importOnce(), 1);
    expect(native.fsyncedPaths, <String>[spool.root.path]);
    expect(
      native.operations,
      <String>['fsync:${spool.root.path}', 'delete:first.json'],
    );
    expect(native.deleted, <String>['first.json']);
    expect(await spool.pending(), hasLength(1));
  });

  test('conditional native deletion preserves a newer overflow replacement', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-replacement-race-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final Uint8List original = observationBytes(sequence: 0);
    final Uint8List replacement = observationBytes(sequence: 1);
    final ReplacingNativeChatMirrorBridge native = ReplacingNativeChatMirrorBridge(
      <NativeChatMirrorRecord>[
        NativeChatMirrorRecord(name: 'native-overflow-session.json', payload: original),
      ],
      replacementPayload: replacement,
    );
    final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
      nativeBridge: native,
      spool: spool,
    );

    expect(await importer.importOnce(), 1);
    expect(native.records, hasLength(1));
    expect(native.records.single.payload, replacement);
    expect(native.deleted, isEmpty);
    expect(await spool.pending(), hasLength(1));

    expect(await importer.importOnce(), 1);
    expect(native.records, isEmpty);
    expect(native.deleted, <String>['native-overflow-session.json']);
    expect(await spool.pending(), hasLength(2));
  });

  test('malformed native observation does not block import or explicit deletion', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-malformed-native-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final Map<String, Object?> wrongType =
        jsonDecode(utf8.decode(observationBytes(sequence: 2))) as Map<String, Object?>;
    wrongType['event_kind'] = 1;
    final FakeNativeChatMirrorBridge native = FakeNativeChatMirrorBridge(
      <NativeChatMirrorRecord>[
        NativeChatMirrorRecord(
          name: 'malformed.json',
          payload: Uint8List.fromList(utf8.encode('{broken')),
        ),
        NativeChatMirrorRecord(
          name: 'wrong_type.json',
          payload: Uint8List.fromList(utf8.encode(jsonEncode(wrongType))),
        ),
        NativeChatMirrorRecord(
          name: 'valid.json',
          payload: observationBytes(sequence: 1),
        ),
      ],
    );
    final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
      nativeBridge: native,
      spool: spool,
    );

    expect(await importer.importOnce(), 1);
    expect(native.deleted, <String>['valid.json']);
    expect(
      native.records.map((NativeChatMirrorRecord record) => record.name),
      <String>['malformed.json', 'wrong_type.json'],
    );
    expect(await spool.pending(), hasLength(1));
    expect(await importer.deleteNativePending(), 2);
    expect(
      native.deleted,
      <String>['valid.json', 'malformed.json', 'wrong_type.json'],
    );
    expect(native.records, isEmpty);
  });

  test('import pagination advances past a full malformed native page', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-malformed-page-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final List<NativeChatMirrorRecord> records = List<NativeChatMirrorRecord>.generate(
      50,
      (int index) => NativeChatMirrorRecord(
        name: "record_${index.toString().padLeft(3, '0')}.json",
        payload: Uint8List.fromList(utf8.encode('{broken')),
      ),
    )
      ..add(
        NativeChatMirrorRecord(
          name: 'record_999.json',
          payload: observationBytes(sequence: 999),
        ),
      );
    final FakeNativeChatMirrorBridge native = FakeNativeChatMirrorBridge(
      records,
      bodyBatchLimit: 50,
    );
    final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
      nativeBridge: native,
      spool: spool,
    );

    expect(await importer.importOnce(), 1);
    expect(native.deleted, <String>['record_999.json']);
    expect(native.records, hasLength(50));
    expect(await spool.pending(), hasLength(1));
  });

  test('explicit native delete removes observations without importing them', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-native-delete-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final FakeNativeChatMirrorBridge native = FakeNativeChatMirrorBridge(
      <NativeChatMirrorRecord>[
        NativeChatMirrorRecord(name: 'first.json', payload: observationBytes()),
        NativeChatMirrorRecord(
          name: 'second.json',
          payload: observationBytes(sequence: 1),
        ),
      ],
    );
    final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
      nativeBridge: native,
      spool: spool,
    );

    expect(await importer.deleteNativePending(), 2);
    expect(native.deleted, <String>['first.json', 'second.json']);
    expect(native.records, isEmpty);
    expect(await spool.pending(), isEmpty);
  });

  test('explicit native delete drains every bounded name batch', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-batched-delete-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final FakeNativeChatMirrorBridge native = FakeNativeChatMirrorBridge(
      List<NativeChatMirrorRecord>.generate(
        1001,
        (int index) => NativeChatMirrorRecord(
          name: 'event_$index.json',
          payload: Uint8List(0),
        ),
      ),
      nameBatchLimit: 1000,
    );
    final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
      nativeBridge: native,
      spool: spool,
    );

    expect(await importer.deleteNativePending(), 1001);
    expect(native.records, isEmpty);
    expect(native.deleted, hasLength(1001));
  });

  test('uploader sends exact payload with separate capability and hash', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-upload-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final PendingChatMirrorEvent pending = await spool.enqueue(observationBytes());
    http.Request? captured;
    final MockClient client = MockClient((http.Request request) async {
      captured = request;
      return http.Response('{"accepted":true}', HttpStatus.accepted);
    });
    final ChatMirrorUploader uploader = ChatMirrorUploader(
      endpoint: ChatMirrorEndpoint.parse('http://100.112.32.64:8790/chat-mirror'),
      receiverToken: 'chat-secret',
      client: client,
    );

    await uploader.upload(pending);
    expect(captured, isNotNull);
    expect(captured!.url.path, '/chat-mirror');
    expect(captured!.url.query, isEmpty);
    expect(captured!.headers['X-Manfred-Chat-Token'], 'chat-secret');
    expect(captured!.headers['Idempotency-Key'], pending.idempotencyKey);
    expect(captured!.headers['X-Manfred-Content-SHA256'], pending.payloadSha256);
    expect(captured!.bodyBytes, await File(pending.payloadPath).readAsBytes());
  });

  test('permanent rejection does not block an independent session', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-rejected-drain-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    await spool.enqueue(observationBytes(sessionId: 'a-rejected'));
    await spool.enqueue(observationBytes(sessionId: 'z-valid'));
    int requests = 0;
    final ChatMirrorUploader uploader = ChatMirrorUploader(
      endpoint: ChatMirrorEndpoint.parse('http://100.112.32.64:8790/chat-mirror'),
      receiverToken: 'chat-secret',
      client: MockClient((http.Request request) async {
        requests++;
        return request.body.contains('a-rejected')
            ? http.Response('{"detail":"conflict"}', HttpStatus.unprocessableEntity)
            : http.Response('{"accepted":true}', HttpStatus.accepted);
      }),
    );
    final Set<String> rejected = <String>{};

    final ChatMirrorDrainResult result = await drainChatMirrorSpool(
      spool: spool,
      uploader: uploader,
      rejectedEventIds: rejected,
    );
    expect(result.uploaded, 1);
    expect(result.newlyRejected, 1);
    expect(result.retainedRejected, 1);
    expect(await spool.pending(), hasLength(1));
    expect((await spool.pending()).single.mirrorSessionId, 'a-rejected');
    expect(requests, 2);

    final ChatMirrorDrainResult repeated = await drainChatMirrorSpool(
      spool: spool,
      uploader: uploader,
      rejectedEventIds: rejected,
    );
    expect(repeated.uploaded, 0);
    expect(repeated.newlyRejected, 0);
    expect(repeated.retainedRejected, 1);
    expect(requests, 2);
    uploader.close();
  });

  test('local payload corruption does not block an independent session', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-corrupt-drain-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final PendingChatMirrorEvent corrupt =
        await spool.enqueue(observationBytes(sessionId: 'a-corrupt'));
    await spool.enqueue(observationBytes(sessionId: 'z-valid'));
    await File(corrupt.payloadPath).writeAsString('{}', flush: true);
    int requests = 0;
    final ChatMirrorUploader uploader = ChatMirrorUploader(
      endpoint: ChatMirrorEndpoint.parse('http://100.112.32.64:8790/chat-mirror'),
      receiverToken: 'chat-secret',
      client: MockClient((http.Request request) async {
        requests++;
        return http.Response('{"accepted":true}', HttpStatus.accepted);
      }),
    );
    final Set<String> rejected = <String>{};

    final ChatMirrorDrainResult result = await drainChatMirrorSpool(
      spool: spool,
      uploader: uploader,
      rejectedEventIds: rejected,
    );
    expect(result.uploaded, 1);
    expect(result.newlyRejected, 1);
    expect(result.retainedRejected, 1);
    expect((await spool.pending()).single.id, corrupt.id);
    expect(requests, 1);
    uploader.close();
  });

  test('drain stop signal prevents queued uploads during deletion', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-stop-drain-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    await spool.enqueue(observationBytes(sessionId: 'delete-before-upload'));
    final ChatMirrorUploader uploader = ChatMirrorUploader(
      endpoint: ChatMirrorEndpoint.parse('http://100.112.32.64:8790/chat-mirror'),
      receiverToken: 'chat-secret',
      client: MockClient((http.Request request) async {
        fail('delete stop signal must prevent upload');
      }),
    );

    final ChatMirrorDrainResult result = await drainChatMirrorSpool(
      spool: spool,
      uploader: uploader,
      rejectedEventIds: <String>{},
      shouldStop: () => true,
    );
    expect(result.uploaded, 0);
    expect(await spool.pending(), hasLength(1));
    uploader.close();
  });

  test('uploader fails closed when committed payload is modified', () async {
    final Directory temp = await Directory.systemTemp.createTemp('chat-mirror-tamper-');
    addTearDown(() => temp.delete(recursive: true));
    final ChatMirrorSpool spool = ChatMirrorSpool(Directory('${temp.path}/spool'));
    final PendingChatMirrorEvent pending = await spool.enqueue(observationBytes());
    await File(pending.payloadPath).writeAsString('{}', flush: true);
    final ChatMirrorUploader uploader = ChatMirrorUploader(
      endpoint: ChatMirrorEndpoint.parse('http://100.112.32.64:8790/chat-mirror'),
      receiverToken: 'chat-secret',
      client: MockClient((http.Request request) async {
        fail('tampered evidence must not be uploaded');
      }),
    );

    await expectLater(
      uploader.upload(pending),
      throwsA(isA<ChatMirrorLocalIntegrityException>()),
    );
  });

  test('method-channel bridge preserves raw native bytes for strict validation', () async {
    const MethodChannel channel = MethodChannel('com.thetopham.manfred_companion/chat_mirror');
    String? deleted;
    final Uint8List corrupt = Uint8List.fromList(<int>[0xff, 0xfe, 0x7b]);
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (MethodCall call) async {
        if (call.method == 'listPending') {
          return <Object?>[
            <String, Object?>{
              'name': 'corrupt.json',
              'body': corrupt,
            },
          ];
        }
        if (call.method == 'listPendingNames') {
          return <Object?>['corrupt.json'];
        }
        if (call.method == 'deletePending') {
          deleted = (call.arguments as Map<Object?, Object?>)['name']! as String;
          return true;
        }
        return null;
      },
    );
    addTearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        channel,
        null,
      );
    });

    final MethodChannelNativeChatMirrorBridge bridge = MethodChannelNativeChatMirrorBridge();
    final NativeChatMirrorRecord record = (await bridge.listPending()).single;
    expect(record.payload, corrupt);
    expect(await bridge.listPendingNames(), <String>['corrupt.json']);
    await bridge.deletePending('corrupt.json');
    expect(deleted, 'corrupt.json');
  });

  test('method-channel bridge rejects unsafe native filenames', () async {
    const MethodChannel channel = MethodChannel('com.thetopham.manfred_companion/chat_mirror');
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      (MethodCall call) async {
        if (call.method == 'listPending') {
          return <Object?>[
            <String, Object?>{
              'name': '../escape.json',
              'body': observationBytes(),
            },
          ];
        }
        return null;
      },
    );
    addTearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        channel,
        null,
      );
    });

    final MethodChannelNativeChatMirrorBridge bridge = MethodChannelNativeChatMirrorBridge();
    await expectLater(bridge.listPending(), throwsFormatException);
  });
}
