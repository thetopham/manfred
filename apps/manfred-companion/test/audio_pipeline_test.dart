import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/audio_pipeline.dart';
import 'package:manfred_companion/src/omi_protocol.dart';

class FakeOpusDecoder implements OpusPacketDecoder {
  Uint8List? lastPacket;

  @override
  Int16List decode(Uint8List packet) {
    lastPacket = packet;
    return Int16List.fromList(<int>[-1, 0, 32767]);
  }

  @override
  void dispose() {}
}

void main() {
  test('codec IDs and the three-byte Omi header match the published protocol', () {
    expect(OmiCodec.fromCharacteristic(<int>[20]), OmiCodec.opus10ms);
    expect(OmiCodec.fromCharacteristic(<int>[21]), OmiCodec.opus20ms);
    expect(stripOmiHeader(<int>[1, 2, 3, 4, 5]), Uint8List.fromList(<int>[4, 5]));
  });

  test('PCM16 notifications preserve little-endian evidence bytes', () {
    final OmiAudioDecoder decoder = OmiAudioDecoder(codec: OmiCodec.pcm16);
    expect(
      decoder.decodeNotification(<int>[9, 8, 7, 0x34, 0x12, 0x78, 0x56]),
      Uint8List.fromList(<int>[0x34, 0x12, 0x78, 0x56]),
    );
  });

  test('Opus notification strips framing and emits explicit PCM16LE', () {
    final FakeOpusDecoder opus = FakeOpusDecoder();
    final OmiAudioDecoder decoder = OmiAudioDecoder(codec: OmiCodec.opus10ms, opusDecoder: opus);
    final Uint8List pcm = decoder.decodeNotification(<int>[0, 0, 1, 10, 11, 12]);
    expect(opus.lastPacket, Uint8List.fromList(<int>[10, 11, 12]));
    expect(pcm, Uint8List.fromList(<int>[0xff, 0xff, 0, 0, 0xff, 0x7f]));
  });

  test('chunker uses a sample clock and carries residual audio forward', () {
    final PcmChunker chunker = PcmChunker();
    final DateTime start = DateTime.utc(2026, 8, 21, 15);
    final Uint8List sixTenths = Uint8List(19200);
    expect(chunker.add(sixTenths, receivedAt: start), isEmpty);
    final List<PcmChunk> chunks = chunker.add(
      sixTenths,
      receivedAt: start.add(const Duration(milliseconds: 600)),
    );
    expect(chunks, hasLength(1));
    expect(chunks.single.sequenceNumber, 0);
    expect(chunks.single.pcm16le.length, 32000);
    expect(chunks.single.captureStartedAt, start);
    expect(chunks.single.captureEndedAt, start.add(const Duration(seconds: 1)));
    final PcmChunk partial = chunker.flush()!;
    expect(partial.sequenceNumber, 1);
    expect(partial.pcm16le.length, 6400);
    expect(partial.captureStartedAt, start.add(const Duration(seconds: 1)));
    expect(partial.captureEndedAt, start.add(const Duration(milliseconds: 1200)));
  });

  test('chunker keeps one sample clock across Omi-sized frames despite receive jitter', () {
    final PcmChunker chunker = PcmChunker();
    final DateTime start = DateTime.utc(2026, 8, 21, 15);
    final List<PcmChunk> emitted = <PcmChunk>[];
    for (int packet = 0; packet < 100; packet++) {
      final int jitterMilliseconds = packet == 0 ? 0 : (packet.isEven ? 35 : 125);
      emitted.addAll(
        chunker.add(
          Uint8List(960 * 2),
          receivedAt: start.add(Duration(milliseconds: packet * 60 + jitterMilliseconds)),
        ),
      );
    }
    expect(emitted, hasLength(6));
    for (int index = 0; index < emitted.length; index++) {
      final PcmChunk chunk = emitted[index];
      expect(chunk.captureStartedAt, start.add(Duration(seconds: index)));
      expect(chunk.captureEndedAt, start.add(Duration(seconds: index + 1)));
      if (index > 0) {
        expect(chunk.captureStartedAt, emitted[index - 1].captureEndedAt);
      }
    }
  });

  test('PCM quality reports peak RMS clipping and DC offset without normalization', () {
    final ByteData data = ByteData(8)
      ..setInt16(0, -32768, Endian.little)
      ..setInt16(2, -1000, Endian.little)
      ..setInt16(4, 1000, Endian.little)
      ..setInt16(6, 32767, Endian.little);
    final PcmQuality quality = PcmQuality.fromPcm16le(data.buffer.asUint8List());

    expect(quality.peakAbs, 32768);
    expect(quality.clippedSamples, 2);
    expect(quality.dcOffset, closeTo(-0.25, 0.000001));
    expect(quality.rms, greaterThan(23000));
  });

  test('chunker refuses to mix decoder generations and labels flushed evidence', () {
    final PcmChunker chunker = PcmChunker();
    final DateTime start = DateTime.utc(2026, 8, 22, 18);
    chunker.add(Uint8List(16000), receivedAt: start, decoderGeneration: 7);

    expect(
      () => chunker.add(
        Uint8List(16000),
        receivedAt: start.add(const Duration(milliseconds: 500)),
        decoderGeneration: 8,
      ),
      throwsStateError,
    );
    final PcmChunk oldGeneration = chunker.flush()!;
    expect(oldGeneration.decoderGeneration, 7);

    final List<PcmChunk> next = chunker.add(
      Uint8List(32000),
      receivedAt: start.add(const Duration(milliseconds: 500)),
      decoderGeneration: 8,
    );
    expect(next.single.decoderGeneration, 8);
  });
}
