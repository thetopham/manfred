import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/omi_protocol.dart';
import 'package:manfred_companion/src/validation_evidence.dart';

void main() {
  test('validation archive preserves opaque Omi packets and decoder generations', () async {
    final Directory temp = await Directory.systemTemp.createTemp('manfred-validation-test-');
    addTearDown(() => temp.delete(recursive: true));
    final ValidationEvidenceArchive archive = ValidationEvidenceArchive(
      root: Directory('${temp.path}/validation'),
      sourceSessionId: 'capture-session',
    );

    await archive.initialize();
    await archive.recordDecoderGeneration(
      generation: 4,
      codec: OmiCodec.opus10ms,
      occurredAt: DateTime.utc(2026, 8, 22, 18),
    );
    await archive.recordPacket(
      packetSequence: 12,
      generation: 4,
      receivedAt: DateTime.utc(2026, 8, 22, 18, 0, 1),
      rawPacket: <int>[1, 2, 3, 4, 5],
      disposition: 'decoded',
    );
    await archive.close();

    final List<String> packetLines = await archive.packetFile.readAsLines();
    expect(packetLines, hasLength(1));
    final Map<String, Object?> packet = jsonDecode(packetLines.single) as Map<String, Object?>;
    expect(packet['packet_sequence'], 12);
    expect(packet['decoder_generation'], 4);
    expect(packet['opaque_header_base64'], base64Encode(<int>[1, 2, 3]));
    expect(packet['raw_packet_base64'], base64Encode(<int>[1, 2, 3, 4, 5]));
    expect(packet['disposition'], 'decoded');

    final List<String> decoderLines = await archive.decoderEventFile.readAsLines();
    expect(decoderLines, hasLength(1));
    final Map<String, Object?> decoder = jsonDecode(decoderLines.single) as Map<String, Object?>;
    expect(decoder['decoder_generation'], 4);
    expect(decoder['codec'], 'opus10ms');
  });

  test('optional validation write failures are counted instead of thrown', () async {
    final Directory temp = await Directory.systemTemp.createTemp('manfred-validation-failure-');
    addTearDown(() => temp.delete(recursive: true));
    final ValidationEvidenceArchive archive = ValidationEvidenceArchive(
      root: Directory('${temp.path}/validation'),
      sourceSessionId: 'capture-session',
    );
    await archive.initialize();
    await archive.close();

    final bool packetRecorded = await archive.tryRecordPacket(
      packetSequence: 0,
      generation: 1,
      receivedAt: DateTime.utc(2026, 8, 22, 18),
      rawPacket: <int>[1, 2, 3, 4],
      disposition: 'decoded',
    );
    final bool generationRecorded = await archive.tryRecordDecoderGeneration(
      generation: 2,
      codec: OmiCodec.opus10ms,
      occurredAt: DateTime.utc(2026, 8, 22, 18, 0, 1),
    );

    expect(packetRecorded, isFalse);
    expect(generationRecorded, isFalse);
    expect(archive.writeErrors, 2);
  });
}
