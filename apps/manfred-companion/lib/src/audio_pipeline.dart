import 'dart:math' as math;
import 'dart:typed_data';

import 'omi_protocol.dart';

abstract interface class OpusPacketDecoder {
  Int16List decode(Uint8List packet);
  void dispose();
}

class OmiAudioDecoder {
  OmiAudioDecoder({required this.codec, this.opusDecoder});

  final OmiCodec codec;
  final OpusPacketDecoder? opusDecoder;

  Uint8List decodeNotification(List<int> notification) {
    final Uint8List payload = stripOmiHeader(notification);
    switch (codec) {
      case OmiCodec.pcm16:
        if (payload.length.isOdd) {
          throw const FormatException('Omi PCM16 payload has an odd byte count');
        }
        return payload;
      case OmiCodec.opus10ms:
      case OmiCodec.opus20ms:
        final OpusPacketDecoder? decoder = opusDecoder;
        if (decoder == null) {
          throw StateError('An Opus decoder is required for the Omi Opus codec');
        }
        return int16ToLittleEndian(decoder.decode(payload));
      case OmiCodec.pcm8:
        throw UnsupportedError(
          'PCM8 conversion is deliberately unsupported until its signedness is verified',
        );
    }
  }

  void dispose() => opusDecoder?.dispose();
}

Uint8List int16ToLittleEndian(Int16List samples) {
  final ByteData data = ByteData(samples.length * 2);
  for (int index = 0; index < samples.length; index++) {
    data.setInt16(index * 2, samples[index], Endian.little);
  }
  return data.buffer.asUint8List();
}

class PcmQuality {
  const PcmQuality({
    required this.peakAbs,
    required this.rms,
    required this.clippedSamples,
    required this.dcOffset,
  });

  final int peakAbs;
  final double rms;
  final int clippedSamples;
  final double dcOffset;

  factory PcmQuality.fromPcm16le(Uint8List pcm16le) {
    if (pcm16le.length.isOdd) {
      throw const FormatException('PCM16 quality requires an even byte count');
    }
    final ByteData data = ByteData.sublistView(pcm16le);
    int peak = 0;
    int clipped = 0;
    double sum = 0;
    double sumSquares = 0;
    final int sampleCount = pcm16le.length ~/ 2;
    for (int index = 0; index < sampleCount; index++) {
      final int sample = data.getInt16(index * 2, Endian.little);
      final int absolute = sample.abs();
      if (absolute > peak) {
        peak = absolute;
      }
      if (sample == -32768 || sample == 32767) {
        clipped++;
      }
      sum += sample;
      sumSquares += sample * sample;
    }
    return PcmQuality(
      peakAbs: peak,
      rms: sampleCount == 0 ? 0 : math.sqrt(sumSquares / sampleCount),
      clippedSamples: clipped,
      dcOffset: sampleCount == 0 ? 0 : sum / sampleCount,
    );
  }

  Map<String, Object> toJson() => <String, Object>{
        'peak_abs': peakAbs,
        'rms': rms,
        'clipped_samples': clippedSamples,
        'dc_offset': dcOffset,
      };
}

class PcmChunk {
  const PcmChunk({
    required this.pcm16le,
    required this.sequenceNumber,
    required this.captureStartedAt,
    required this.captureEndedAt,
    this.decoderGeneration = 0,
  });

  final Uint8List pcm16le;
  final int sequenceNumber;
  final DateTime captureStartedAt;
  final DateTime captureEndedAt;
  final int decoderGeneration;

  double get durationSeconds => pcm16le.length / (omiPcmSampleRate * 2);
  PcmQuality get quality => PcmQuality.fromPcm16le(pcm16le);
}

class PcmChunker {
  PcmChunker({this.targetSeconds = 1.0})
      : assert(targetSeconds > 0),
        targetBytes = (omiPcmSampleRate * 2 * targetSeconds).round();

  final double targetSeconds;
  final int targetBytes;
  final List<int> _buffer = <int>[];
  int _nextSequence = 0;
  DateTime? _captureAnchor;
  int _emittedSamples = 0;
  int? _bufferGeneration;

  List<PcmChunk> add(
    Uint8List pcm16le, {
    required DateTime receivedAt,
    int decoderGeneration = 0,
  }) {
    if (pcm16le.length.isOdd) {
      throw const FormatException('PCM16 data must contain an even number of bytes');
    }
    if (pcm16le.isEmpty) {
      return const <PcmChunk>[];
    }
    if (_buffer.isNotEmpty && _bufferGeneration != decoderGeneration) {
      throw StateError('Flush PCM buffered under the old decoder generation before adding new audio');
    }
    _captureAnchor ??= receivedAt.toUtc();
    _bufferGeneration ??= decoderGeneration;
    _buffer.addAll(pcm16le);
    final List<PcmChunk> emitted = <PcmChunk>[];
    while (_buffer.length >= targetBytes) {
      final Uint8List body = Uint8List.fromList(_buffer.sublist(0, targetBytes));
      _buffer.removeRange(0, targetBytes);
      emitted.add(_makeChunk(body));
    }
    if (_buffer.isEmpty) {
      _bufferGeneration = null;
    }
    return emitted;
  }

  PcmChunk? flush() {
    if (_buffer.isEmpty) {
      return null;
    }
    final Uint8List body = Uint8List.fromList(_buffer);
    _buffer.clear();
    final PcmChunk chunk = _makeChunk(body);
    _bufferGeneration = null;
    return chunk;
  }

  PcmChunk _makeChunk(Uint8List body) {
    final DateTime anchor = _captureAnchor!;
    final int startedAtMicroseconds =
        (_emittedSamples / omiPcmSampleRate * Duration.microsecondsPerSecond).round();
    _emittedSamples += body.length ~/ 2;
    final int endedAtMicroseconds =
        (_emittedSamples / omiPcmSampleRate * Duration.microsecondsPerSecond).round();
    final DateTime startedAt = anchor.add(Duration(microseconds: startedAtMicroseconds));
    final DateTime endedAt = anchor.add(Duration(microseconds: endedAtMicroseconds));
    final PcmChunk chunk = PcmChunk(
      pcm16le: body,
      sequenceNumber: _nextSequence++,
      captureStartedAt: startedAt,
      captureEndedAt: endedAt,
      decoderGeneration: _bufferGeneration ?? 0,
    );
    return chunk;
  }

  int get pendingBytes => _buffer.length;
}
