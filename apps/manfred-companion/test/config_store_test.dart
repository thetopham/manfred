import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/config_store.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    FlutterSecureStorage.setMockInitialValues(<String, String>{});
  });

  test('endpoint and token survive one secure config round trip', () async {
    const CompanionConfig expected = CompanionConfig(
      endpoint: 'http://100.64.0.10:8787/audio',
      receiverToken: 'receiver-token',
      deviceId: 'omi-device',
      validationCaptureEnabled: true,
    );

    await ConfigStore().save(expected);
    final CompanionConfig actual = await ConfigStore().load();

    expect(actual.endpoint, expected.endpoint);
    expect(actual.receiverToken, expected.receiverToken);
    expect(actual.deviceId, expected.deviceId);
    expect(actual.validationCaptureEnabled, isTrue);
  });

  test('legacy split settings migrate into the secure config envelope', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      'manfred_endpoint': 'http://100.64.0.20:8787/audio',
      'omi_device_id': 'legacy-omi',
    });
    FlutterSecureStorage.setMockInitialValues(<String, String>{
      'manfred_receiver_token': 'legacy-token',
    });

    final CompanionConfig migrated = await ConfigStore().load();
    expect(migrated.endpoint, 'http://100.64.0.20:8787/audio');
    expect(migrated.receiverToken, 'legacy-token');
    expect(migrated.deviceId, 'legacy-omi');
    expect(migrated.validationCaptureEnabled, isFalse);

    final SharedPreferences preferences = await SharedPreferences.getInstance();
    await preferences.setString('manfred_endpoint', 'http://100.64.0.99:8787/audio');
    await const FlutterSecureStorage().write(
      key: 'manfred_receiver_token',
      value: 'stale-legacy-token',
    );

    final CompanionConfig reloaded = await ConfigStore().load();
    expect(reloaded.endpoint, migrated.endpoint);
    expect(reloaded.receiverToken, migrated.receiverToken);
    expect(reloaded.deviceId, migrated.deviceId);
  });
}
