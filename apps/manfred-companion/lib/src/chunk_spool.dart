import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';

import 'audio_pipeline.dart';

class PendingChunk {
  const PendingChunk({
    required this.id,
    required this.sourceSessionId,
    required this.sourceUid,
    required this.sequenceNumber,
    required this.captureStartedAt,
    required this.captureEndedAt,
    required this.idempotencyKey,
    required this.pcmSha256,
    required this.pcmPath,
    this.decoderGeneration,
    this.pcmPeakAbs,
    this.pcmRms,
    this.pcmClippedSamples,
    this.pcmDcOffset,
  });

  final String id;
  final String sourceSessionId;
  final String sourceUid;
  final int sequenceNumber;
  final DateTime captureStartedAt;
  final DateTime captureEndedAt;
  final String idempotencyKey;
  final String pcmSha256;
  final String pcmPath;
  final int? decoderGeneration;
  final int? pcmPeakAbs;
  final double? pcmRms;
  final int? pcmClippedSamples;
  final double? pcmDcOffset;

  Map<String, Object> toJson() => <String, Object>{
        'id': id,
        'source_session_id': sourceSessionId,
        'source_uid': sourceUid,
        'sequence_number': sequenceNumber,
        'capture_started_at': captureStartedAt.toUtc().toIso8601String(),
        'capture_ended_at': captureEndedAt.toUtc().toIso8601String(),
        'idempotency_key': idempotencyKey,
        'pcm_sha256': pcmSha256,
        'pcm_path': pcmPath,
        if (decoderGeneration != null) 'decoder_generation': decoderGeneration!,
        if (pcmPeakAbs != null) 'pcm_peak_abs': pcmPeakAbs!,
        if (pcmRms != null) 'pcm_rms': pcmRms!,
        if (pcmClippedSamples != null) 'pcm_clipped_samples': pcmClippedSamples!,
        if (pcmDcOffset != null) 'pcm_dc_offset': pcmDcOffset!,
      };

  factory PendingChunk.fromJson(Map<String, Object?> json) {
    final String idempotencyKey = json['idempotency_key']! as String;
    return PendingChunk(
      id: json['id']! as String,
      sourceSessionId: json['source_session_id']! as String,
      sourceUid: json['source_uid']! as String,
      sequenceNumber: json['sequence_number']! as int,
      captureStartedAt: DateTime.parse(json['capture_started_at']! as String).toUtc(),
      captureEndedAt: DateTime.parse(json['capture_ended_at']! as String).toUtc(),
      idempotencyKey: idempotencyKey,
      pcmSha256: json['pcm_sha256'] as String? ?? idempotencyKey.split(':').last,
      pcmPath: json['pcm_path']! as String,
      decoderGeneration: json['decoder_generation'] as int?,
      pcmPeakAbs: json['pcm_peak_abs'] as int?,
      pcmRms: (json['pcm_rms'] as num?)?.toDouble(),
      pcmClippedSamples: json['pcm_clipped_samples'] as int?,
      pcmDcOffset: (json['pcm_dc_offset'] as num?)?.toDouble(),
    );
  }
}

class ChunkSpool {
  ChunkSpool(this.root);

  final Directory root;
  Future<void>? _initializationFuture;

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
      if (entity is! File || !entity.path.endsWith('.json.tmp')) {
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
        final PendingChunk pending = PendingChunk.fromJson(decoded);
        final String expectedPcmPath = '${root.path}/${pending.id}.pcm';
        if (pending.pcmPath != expectedPcmPath) {
          continue;
        }
        final File committedPcm = File(expectedPcmPath);
        final File temporaryPcm = File('$expectedPcmPath.tmp');
        final File? candidate = await committedPcm.exists()
            ? committedPcm
            : (await temporaryPcm.exists() ? temporaryPcm : null);
        if (candidate == null) {
          continue;
        }
        final String candidateHash = sha256.convert(await candidate.readAsBytes()).toString();
        if (candidateHash != pending.pcmSha256) {
          continue;
        }
        if (candidate.path == temporaryPcm.path) {
          await temporaryPcm.rename(committedPcm.path);
        }
        await entity.rename(committedMetadata.path);
      } on FormatException {
        // Preserve malformed/interrupted evidence for explicit inspection.
      } on TypeError {
        // Preserve malformed/interrupted evidence for explicit inspection.
      }
    }
  }

  Future<PendingChunk> enqueue(String sourceSessionId, String sourceUid, PcmChunk chunk) async {
    await initialize();
    final String audioHash = sha256.convert(chunk.pcm16le).toString();
    final String idempotencyKey = '$sourceSessionId:${chunk.sequenceNumber}:$audioHash';
    final String id = sha256.convert(utf8.encode(idempotencyKey)).toString().substring(0, 32);
    final PcmQuality quality = chunk.quality;
    final File pcmFile = File('${root.path}/$id.pcm');
    final File metadataFile = File('${root.path}/$id.json');
    final PendingChunk pending = PendingChunk(
      id: id,
      sourceSessionId: sourceSessionId,
      sourceUid: sourceUid,
      sequenceNumber: chunk.sequenceNumber,
      captureStartedAt: chunk.captureStartedAt,
      captureEndedAt: chunk.captureEndedAt,
      idempotencyKey: idempotencyKey,
      pcmSha256: audioHash,
      pcmPath: pcmFile.path,
      decoderGeneration: chunk.decoderGeneration,
      pcmPeakAbs: quality.peakAbs,
      pcmRms: quality.rms,
      pcmClippedSamples: quality.clippedSamples,
      pcmDcOffset: quality.dcOffset,
    );

    final File pcmTemporary = File('${pcmFile.path}.tmp');
    final File metadataTemporary = File('${metadataFile.path}.tmp');
    await metadataTemporary.writeAsString(jsonEncode(pending.toJson()), flush: true);
    await pcmTemporary.writeAsBytes(chunk.pcm16le, flush: true);
    await pcmTemporary.rename(pcmFile.path);
    await metadataTemporary.rename(metadataFile.path);
    return pending;
  }

  Future<List<PendingChunk>> pending() async {
    await initialize();
    final List<PendingChunk> chunks = <PendingChunk>[];
    await for (final FileSystemEntity entity in root.list()) {
      if (entity is! File || !entity.path.endsWith('.json')) {
        continue;
      }
      final Object? decoded = jsonDecode(await entity.readAsString());
      if (decoded is! Map<String, Object?>) {
        continue;
      }
      final PendingChunk chunk = PendingChunk.fromJson(decoded);
      if (await File(chunk.pcmPath).exists()) {
        chunks.add(chunk);
      }
    }
    chunks.sort((PendingChunk a, PendingChunk b) {
      final int bySession = a.sourceSessionId.compareTo(b.sourceSessionId);
      return bySession != 0 ? bySession : a.sequenceNumber.compareTo(b.sequenceNumber);
    });
    return chunks;
  }

  Future<Uint8List> readPcm(PendingChunk chunk) async => File(chunk.pcmPath).readAsBytes();

  Future<void> remove(PendingChunk chunk) async {
    for (final String path in <String>[chunk.pcmPath, '${root.path}/${chunk.id}.json']) {
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
