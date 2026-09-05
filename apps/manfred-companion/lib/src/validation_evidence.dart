import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

import 'omi_protocol.dart';

/// Optional phone-local evidence retained for supervised BLE/codec validation.
///
/// Header bytes stay opaque. No packet sequence/checksum semantics are inferred
/// until Omi documents them; the complete notification is preserved verbatim.
class ValidationEvidenceArchive {
  ValidationEvidenceArchive({
    required this.root,
    required this.sourceSessionId,
  });

  final Directory root;
  final String sourceSessionId;
  bool _initialized = false;
  bool _closed = false;
  RandomAccessFile? _packetHandle;
  RandomAccessFile? _decoderEventHandle;
  int _writeErrors = 0;

  int get writeErrors => _writeErrors;

  Directory get sessionDirectory => Directory('${root.path}/$sourceSessionId');
  File get manifestFile => File('${sessionDirectory.path}/manifest.json');
  File get packetFile => File('${sessionDirectory.path}/packets.jsonl');
  File get decoderEventFile => File('${sessionDirectory.path}/decoder-events.jsonl');

  Future<void> initialize() async {
    if (_initialized) {
      return;
    }
    if (_closed) {
      throw StateError('Validation evidence archive is already closed');
    }
    if (!RegExp(r'^[A-Za-z0-9._:-]{1,128}$').hasMatch(sourceSessionId)) {
      throw const FormatException('Validation capture session id is invalid');
    }
    await sessionDirectory.create(recursive: true);
    if (!await manifestFile.exists()) {
      await manifestFile.writeAsString(
        '${jsonEncode(<String, Object>{
          'schema_version': 1,
          'source_session_id': sourceSessionId,
          'packet_format': 'jsonl-base64-complete-ble-notification',
          'omi_header_semantics': 'opaque-three-byte-prefix',
          'retention': 'phone-local-until-explicit-delete',
        })}\n',
        flush: true,
      );
    }
    try {
      _packetHandle = await packetFile.open(mode: FileMode.append);
      _decoderEventHandle = await decoderEventFile.open(mode: FileMode.append);
    } catch (_) {
      await _packetHandle?.close();
      _packetHandle = null;
      rethrow;
    }
    _initialized = true;
  }

  Future<void> recordDecoderGeneration({
    required int generation,
    required OmiCodec codec,
    required DateTime occurredAt,
  }) async {
    await initialize();
    final RandomAccessFile handle = _decoderEventHandle!;
    await handle.writeString(
      '${jsonEncode(<String, Object>{
        'event': 'decoder_generation_started',
        'decoder_generation': generation,
        'codec': codec.name,
        'occurred_at': occurredAt.toUtc().toIso8601String(),
      })}\n',
    );
    await handle.flush();
  }

  Future<bool> tryRecordDecoderGeneration({
    required int generation,
    required OmiCodec codec,
    required DateTime occurredAt,
  }) async {
    try {
      await recordDecoderGeneration(
        generation: generation,
        codec: codec,
        occurredAt: occurredAt,
      );
      return true;
    } catch (_) {
      _writeErrors++;
      return false;
    }
  }

  Future<void> recordPacket({
    required int packetSequence,
    required int generation,
    required DateTime receivedAt,
    required List<int> rawPacket,
    required String disposition,
  }) async {
    await initialize();
    final List<int> packet = List<int>.unmodifiable(rawPacket);
    final int headerLength = packet.length < omiPacketHeaderBytes ? packet.length : omiPacketHeaderBytes;
    final List<int> header = packet.sublist(0, headerLength);
    final List<int> payload = packet.sublist(headerLength);
    final RandomAccessFile handle = _packetHandle!;
    await handle.writeString(
      '${jsonEncode(<String, Object>{
        'packet_sequence': packetSequence,
        'decoder_generation': generation,
        'received_at': receivedAt.toUtc().toIso8601String(),
        'disposition': disposition,
        'header_complete': headerLength == omiPacketHeaderBytes,
        'opaque_header_base64': base64Encode(header),
        'raw_packet_base64': base64Encode(packet),
        'raw_packet_sha256': sha256.convert(packet).toString(),
        'payload_bytes': payload.length,
        'payload_sha256': sha256.convert(payload).toString(),
      })}\n',
    );
    await handle.flush();
  }

  Future<bool> tryRecordPacket({
    required int packetSequence,
    required int generation,
    required DateTime receivedAt,
    required List<int> rawPacket,
    required String disposition,
  }) async {
    try {
      await recordPacket(
        packetSequence: packetSequence,
        generation: generation,
        receivedAt: receivedAt,
        rawPacket: rawPacket,
        disposition: disposition,
      );
      return true;
    } catch (_) {
      _writeErrors++;
      return false;
    }
  }

  Future<void> close() async {
    if (_closed) {
      return;
    }
    await _packetHandle?.close();
    await _decoderEventHandle?.close();
    _packetHandle = null;
    _decoderEventHandle = null;
    _initialized = false;
    _closed = true;
  }
}
