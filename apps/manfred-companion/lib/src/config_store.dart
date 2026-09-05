import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

class CompanionConfig {
  const CompanionConfig({
    required this.endpoint,
    required this.receiverToken,
    this.chatMirrorEndpoint = '',
    this.chatMirrorToken = '',
    this.deviceId,
    this.validationCaptureEnabled = false,
  });

  final String endpoint;
  final String receiverToken;
  final String chatMirrorEndpoint;
  final String chatMirrorToken;
  final String? deviceId;
  final bool validationCaptureEnabled;

  bool get isComplete => endpoint.trim().isNotEmpty && receiverToken.trim().isNotEmpty;
  bool get isChatMirrorComplete =>
      chatMirrorEndpoint.trim().isNotEmpty && chatMirrorToken.trim().isNotEmpty;
}

class ConfigStore {
  ConfigStore({FlutterSecureStorage? secureStorage})
      : secureStorage = secureStorage ?? const FlutterSecureStorage();

  static const String _configKey = 'manfred_config_v1';
  static const String _legacyEndpointKey = 'manfred_endpoint';
  static const String _legacyDeviceKey = 'omi_device_id';
  static const String _legacyTokenKey = 'manfred_receiver_token';

  final FlutterSecureStorage secureStorage;

  Future<CompanionConfig> load() async {
    final String? encoded = await secureStorage.read(key: _configKey);
    if (encoded != null) {
      final Object? decoded = jsonDecode(encoded);
      if (decoded is! Map<String, dynamic>) {
        throw const FormatException('Saved Manfred configuration is invalid');
      }
      return CompanionConfig(
        endpoint: decoded['endpoint'] as String? ?? '',
        receiverToken: decoded['receiverToken'] as String? ?? '',
        chatMirrorEndpoint: decoded['chatMirrorEndpoint'] as String? ?? '',
        chatMirrorToken: decoded['chatMirrorToken'] as String? ?? '',
        deviceId: decoded['deviceId'] as String?,
        validationCaptureEnabled: decoded['validationCaptureEnabled'] as bool? ?? false,
      );
    }

    final SharedPreferences preferences = await SharedPreferences.getInstance();
    final CompanionConfig legacyConfig = CompanionConfig(
      endpoint: preferences.getString(_legacyEndpointKey) ?? '',
      receiverToken: await secureStorage.read(key: _legacyTokenKey) ?? '',
      deviceId: preferences.getString(_legacyDeviceKey),
    );
    if (legacyConfig.endpoint.isNotEmpty ||
        legacyConfig.receiverToken.isNotEmpty ||
        legacyConfig.deviceId != null) {
      await save(legacyConfig);
    }
    return legacyConfig;
  }

  Future<void> save(CompanionConfig config) => secureStorage.write(
        key: _configKey,
        value: jsonEncode(<String, Object?>{
          'endpoint': config.endpoint.trim(),
          'receiverToken': config.receiverToken.trim(),
          'chatMirrorEndpoint': config.chatMirrorEndpoint.trim(),
          'chatMirrorToken': config.chatMirrorToken.trim(),
          'deviceId': config.deviceId,
          'validationCaptureEnabled': config.validationCaptureEnabled,
        }),
      );
}
