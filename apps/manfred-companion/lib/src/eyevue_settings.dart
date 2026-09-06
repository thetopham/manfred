import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';

abstract interface class EyevueSettings {
  Future<String?> loadAddress();
  Future<void> saveAddress(String address);
  Future<String?> loadPhotoSource();
  Future<void> savePhotoSource(String source);
}

class SharedPreferencesEyevueSettings implements EyevueSettings {
  static const String addressKey = 'manfred_eyevue_address_v1';
  static const String photoSourceKey = 'manfred_eyevue_photo_source_v1';

  @override
  Future<String?> loadAddress() async =>
      (await SharedPreferences.getInstance()).getString(addressKey);

  @override
  Future<void> saveAddress(String address) async {
    await (await SharedPreferences.getInstance()).setString(addressKey, address);
  }

  @override
  Future<String?> loadPhotoSource() async =>
      (await SharedPreferences.getInstance()).getString(photoSourceKey);

  @override
  Future<void> savePhotoSource(String source) async {
    await (await SharedPreferences.getInstance()).setString(photoSourceKey, source);
  }
}

abstract interface class EyevuePermissionGate {
  Future<void> requestBluetooth(int? androidSdkInt);
  Future<void> requestSession(int? androidSdkInt, {bool usesWifi = true});
  Future<bool> hasWifiDiscoveryPermission();
  Future<bool> requestWifiDiscoveryPermission();
  Future<bool> isWifiDiscoveryLocationEnabled();
}

class AndroidEyevuePermissionGate implements EyevuePermissionGate {
  @override
  Future<bool> hasWifiDiscoveryPermission() async =>
      (await Permission.locationWhenInUse.status).isGranted;

  @override
  Future<bool> requestWifiDiscoveryPermission() async {
    await Permission.locationWhenInUse.request();
    // Recheck both manifest permissions: a request callback may reflect only
    // coarse location, which cannot authorize Wi-Fi scan results on Android.
    return hasWifiDiscoveryPermission();
  }

  @override
  Future<bool> isWifiDiscoveryLocationEnabled() async =>
      (await Permission.locationWhenInUse.serviceStatus).isEnabled;

  Future<void> _request(List<Permission> permissions) async {
    final Map<Permission, PermissionStatus> result = await permissions.request();
    if (result.entries.any(
      (MapEntry<Permission, PermissionStatus> entry) =>
          entry.key != Permission.notification && !entry.value.isGranted,
    )) {
      throw StateError('Allow Bluetooth and nearby-device permissions to use EyeVue.');
    }
  }

  @override
  Future<void> requestBluetooth(int? androidSdkInt) => _request(<Permission>[
        Permission.bluetoothScan,
        Permission.bluetoothConnect,
        if (androidSdkInt != null && androidSdkInt <= 30)
          Permission.locationWhenInUse,
      ]);

  @override
  Future<void> requestSession(int? androidSdkInt, {bool usesWifi = true}) async {
    if (androidSdkInt == null) {
      throw StateError('Android version is unavailable; reconnect EyeVue and retry.');
    }
    await _request(<Permission>[
      Permission.bluetoothConnect,
      if (usesWifi && androidSdkInt >= 33) Permission.nearbyWifiDevices,
      if (androidSdkInt <= 30 || (usesWifi && androidSdkInt <= 32))
        Permission.locationWhenInUse,
      Permission.notification,
    ]);
  }
}
