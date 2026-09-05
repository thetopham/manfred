import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:manfred_companion/src/audio_pipeline.dart';
import 'package:manfred_companion/src/chunk_spool.dart';
import 'package:manfred_companion/src/manfred_uploader.dart';

void main() {
  test('endpoint policy allows tailnet destinations and rejects public HTTP', () {
    expect(ManfredEndpoint.parse('http://100.64.0.1:8787/audio').uri.host, '100.64.0.1');
    expect(ManfredEndpoint.parse('https://demerzel.example.ts.net/audio').uri.scheme, 'https');
    expect(() => ManfredEndpoint.parse('http://192.168.1.2:8787/audio'), throwsFormatException);
    expect(() => ManfredEndpoint.parse('https://example.com/audio'), throwsFormatException);
    expect(() => ManfredEndpoint.parse('http://100.64.0.1:8787/v1/status'), throwsFormatException);
    expect(() => ManfredEndpoint.parse('http://100.64.0.1:8787/audio?token=leak'), throwsFormatException);
  });

  test('spool is durable and uploader sends the direct capture contract', () async {
    final Directory temp = await Directory.systemTemp.createTemp('manfred-spool-test-');
    addTearDown(() => temp.delete(recursive: true));
    final ChunkSpool spool = ChunkSpool(Directory('${temp.path}/spool'));
    final DateTime started = DateTime.utc(2026, 8, 21, 15);
    final PcmChunk chunk = PcmChunk(
      pcm16le: Uint8List(32000),
      sequenceNumber: 7,
      captureStartedAt: started,
      captureEndedAt: started.add(const Duration(seconds: 1)),
    );
    final String sourceUid = pseudonymousSourceUid('omi-device-id');
    final PendingChunk pending = await spool.enqueue('capture-session', sourceUid, chunk);
    expect(await spool.pending(), hasLength(1));

    http.Request? captured;
    final MockClient client = MockClient((http.Request request) async {
      captured = request;
      return http.Response('{"accepted":true}', HttpStatus.accepted);
    });
    final ManfredUploader uploader = ManfredUploader(
      endpoint: ManfredEndpoint.parse('http://100.126.233.3:8787/audio'),
      receiverToken: 'receiver-secret',
      client: client,
    );
    await uploader.upload(pending);
    expect(captured, isNotNull);
    expect(captured!.url.queryParameters['sample_rate'], '16000');
    expect(captured!.url.queryParameters['uid'], sha256.convert(utf8.encode('omi-device-id')).toString());
    expect(captured!.url.queryParameters.containsKey('token'), isFalse);
    expect(captured!.headers['X-Manfred-Token'], 'receiver-secret');
    expect(captured!.headers['X-Manfred-Capture-Session'], 'capture-session');
    expect(captured!.headers['X-Manfred-Sequence'], '7');
    expect(captured!.headers['X-Manfred-Codec'], 'pcm16le');
    expect(captured!.headers['X-Manfred-Transport'], 's25-direct-ble-tailscale');
    expect(captured!.headers['X-Manfred-Content-SHA256'], pending.pcmSha256);
    expect(captured!.headers['X-Manfred-Decoder-Generation'], '0');
    expect(captured!.bodyBytes, hasLength(32000));

    final ManfredUploader slowUploader = ManfredUploader(
      endpoint: ManfredEndpoint.parse('http://100.126.233.3:8787/audio'),
      receiverToken: 'receiver-secret',
      requestTimeout: const Duration(milliseconds: 1),
      client: MockClient((http.Request request) async {
        await Future<void>.delayed(const Duration(milliseconds: 20));
        return http.Response('{}', HttpStatus.accepted);
      }),
    );
    await expectLater(slowUploader.upload(pending), throwsA(isA<TimeoutException>()));

    await spool.remove(pending);
    expect(await spool.pending(), isEmpty);
  });

  test('uploader fails closed when committed spool bytes no longer match metadata', () async {
    final Directory temp = await Directory.systemTemp.createTemp('manfred-spool-tamper-');
    addTearDown(() => temp.delete(recursive: true));
    final ChunkSpool spool = ChunkSpool(Directory('${temp.path}/spool'));
    final DateTime started = DateTime.utc(2026, 8, 22, 18);
    final PendingChunk pending = await spool.enqueue(
      'tamper-session',
      pseudonymousSourceUid('omi-device-id'),
      PcmChunk(
        pcm16le: Uint8List(32000),
        sequenceNumber: 0,
        captureStartedAt: started,
        captureEndedAt: started.add(const Duration(seconds: 1)),
      ),
    );
    await File(pending.pcmPath).writeAsBytes(Uint8List.fromList(<int>[1, 2, 3, 4]), flush: true);
    bool called = false;
    final ManfredUploader uploader = ManfredUploader(
      endpoint: ManfredEndpoint.parse('http://100.126.233.3:8787/audio'),
      receiverToken: 'receiver-secret',
      client: MockClient((http.Request request) async {
        called = true;
        return http.Response('{}', HttpStatus.accepted);
      }),
    );

    await expectLater(uploader.upload(pending), throwsStateError);
    expect(called, isFalse);
    expect(await spool.pending(), hasLength(1));
  });

  test('spool recovers a process kill between PCM and metadata commit', () async {
    final Directory temp = await Directory.systemTemp.createTemp('manfred-spool-recovery-');
    addTearDown(() => temp.delete(recursive: true));
    final Directory root = Directory('${temp.path}/spool');
    final ChunkSpool spool = ChunkSpool(root);
    final DateTime started = DateTime.utc(2026, 8, 21, 15);
    final PcmChunk chunk = PcmChunk(
      pcm16le: Uint8List.fromList(List<int>.generate(32000, (int index) => index % 251)),
      sequenceNumber: 8,
      captureStartedAt: started,
      captureEndedAt: started.add(const Duration(seconds: 1)),
    );
    final PendingChunk pending = await spool.enqueue(
      'recovery-session',
      pseudonymousSourceUid('omi-device-id'),
      chunk,
    );
    final File committedMetadata = File('${root.path}/${pending.id}.json');
    final File interruptedMetadata = File('${committedMetadata.path}.tmp');
    await committedMetadata.rename(interruptedMetadata.path);
    expect(await committedMetadata.exists(), isFalse);
    expect(await File(pending.pcmPath).exists(), isTrue);

    final ChunkSpool restarted = ChunkSpool(root);
    final List<PendingChunk> recovered = await restarted.pending();
    expect(recovered, hasLength(1));
    expect(recovered.single.id, pending.id);
    expect(await restarted.readPcm(recovered.single), chunk.pcm16le);
    expect(await committedMetadata.exists(), isTrue);
    expect(await interruptedMetadata.exists(), isFalse);
  });

  test('normal pending scans do not reconcile an active enqueue transaction', () async {
    final Directory temp = await Directory.systemTemp.createTemp('manfred-spool-active-');
    addTearDown(() => temp.delete(recursive: true));
    final Directory root = Directory('${temp.path}/spool');
    final ChunkSpool spool = ChunkSpool(root);
    await spool.initialize();

    final Uint8List pcm = Uint8List.fromList(List<int>.generate(32000, (int index) => index % 239));
    final String pcmHash = sha256.convert(pcm).toString();
    final String idempotencyKey = 'active-session:9:$pcmHash';
    final String id = sha256.convert(utf8.encode(idempotencyKey)).toString().substring(0, 32);
    final File committedPcm = File('${root.path}/$id.pcm');
    final File temporaryPcm = File('${committedPcm.path}.tmp');
    final File committedMetadata = File('${root.path}/$id.json');
    final File temporaryMetadata = File('${committedMetadata.path}.tmp');
    final DateTime started = DateTime.utc(2026, 8, 21, 15);
    final PendingChunk active = PendingChunk(
      id: id,
      sourceSessionId: 'active-session',
      sourceUid: pseudonymousSourceUid('omi-device-id'),
      sequenceNumber: 9,
      captureStartedAt: started,
      captureEndedAt: started.add(const Duration(seconds: 1)),
      idempotencyKey: idempotencyKey,
      pcmSha256: pcmHash,
      pcmPath: committedPcm.path,
    );
    await temporaryMetadata.writeAsString(jsonEncode(active.toJson()), flush: true);
    await temporaryPcm.writeAsBytes(pcm, flush: true);

    expect(await spool.pending(), isEmpty);
    expect(await temporaryMetadata.exists(), isTrue);
    expect(await temporaryPcm.exists(), isTrue);
    expect(await committedMetadata.exists(), isFalse);
    expect(await committedPcm.exists(), isFalse);

    await temporaryPcm.rename(committedPcm.path);
    await temporaryMetadata.rename(committedMetadata.path);
    final List<PendingChunk> recovered = await spool.pending();
    expect(recovered, hasLength(1));
    expect(recovered.single.decoderGeneration, isNull);
  });
}
