import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;

import 'chunk_spool.dart';
import 'omi_protocol.dart';

class ManfredEndpoint {
  const ManfredEndpoint(this.uri);

  final Uri uri;

  factory ManfredEndpoint.parse(String value) {
    final Uri uri = Uri.parse(value.trim());
    if (uri.path != '/audio' || uri.fragment.isNotEmpty || uri.hasQuery) {
      throw const FormatException('Endpoint must be exactly /audio with no query or fragment');
    }
    final bool tailnetIp = _isTailscaleIpv4(uri.host);
    final bool tailnetDns = uri.host.toLowerCase().endsWith('.ts.net');
    final bool allowed = (uri.scheme == 'http' && tailnetIp) ||
        (uri.scheme == 'https' && (tailnetIp || tailnetDns));
    if (!allowed) {
      throw const FormatException('Endpoint must be a Tailscale 100.64.0.0/10 address or .ts.net HTTPS host');
    }
    return ManfredEndpoint(uri);
  }

  static bool _isTailscaleIpv4(String host) {
    final List<int>? octets = _ipv4(host);
    return octets != null && octets[0] == 100 && octets[1] >= 64 && octets[1] <= 127;
  }

  static List<int>? _ipv4(String host) {
    final List<String> parts = host.split('.');
    if (parts.length != 4) {
      return null;
    }
    final List<int> values = <int>[];
    for (final String part in parts) {
      final int? value = int.tryParse(part);
      if (value == null || value < 0 || value > 255) {
        return null;
      }
      values.add(value);
    }
    return values;
  }
}

String pseudonymousSourceUid(String deviceId) => sha256.convert(utf8.encode(deviceId)).toString();

class ManfredUploader {
  ManfredUploader({
    required this.endpoint,
    required this.receiverToken,
    http.Client? client,
    this.requestTimeout = const Duration(seconds: 10),
  }) : client = client ?? http.Client();

  final ManfredEndpoint endpoint;
  final String receiverToken;

  final http.Client client;
  final Duration requestTimeout;

  Future<void> upload(PendingChunk chunk) async {
    final List<int> body = await File(chunk.pcmPath).readAsBytes();
    final String currentHash = sha256.convert(body).toString();
    if (currentHash != chunk.pcmSha256) {
      throw StateError('Committed spool PCM no longer matches its SHA-256 metadata');
    }
    final Uri uri = endpoint.uri.replace(
      queryParameters: <String, String>{
        ...endpoint.uri.queryParameters,
        'sample_rate': '$omiPcmSampleRate',
        'uid': chunk.sourceUid,
      },
    );
    final http.Response response = await client.post(
      uri,
      headers: <String, String>{
        HttpHeaders.contentTypeHeader: 'application/octet-stream',
        'X-Manfred-Token': receiverToken,
        'Idempotency-Key': chunk.idempotencyKey,
        'X-Manfred-Content-SHA256': currentHash,
        'X-Manfred-Capture-Session': chunk.sourceSessionId,
        'X-Manfred-Sequence': '${chunk.sequenceNumber}',
        'X-Manfred-Capture-Started-At': chunk.captureStartedAt.toUtc().toIso8601String(),
        'X-Manfred-Capture-Ended-At': chunk.captureEndedAt.toUtc().toIso8601String(),
        'X-Manfred-Codec': 'pcm16le',
        'X-Manfred-Transport': 's25-direct-ble-tailscale',
        if (chunk.decoderGeneration != null)
          'X-Manfred-Decoder-Generation': '${chunk.decoderGeneration}',
      },
      body: body,
    ).timeout(requestTimeout);
    if (response.statusCode != HttpStatus.accepted) {
      throw HttpException(
        'Manfred Ears rejected chunk with HTTP ${response.statusCode}',
        uri: endpoint.uri,
      );
    }
  }

  void close() => client.close();
}
