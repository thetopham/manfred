import 'package:flutter_foreground_task/flutter_foreground_task.dart';

void initializeForegroundCommunication() {
  FlutterForegroundTask.initCommunicationPort();
}

@pragma('vm:entry-point')
void manfredForegroundCallback() {
  FlutterForegroundTask.setTaskHandler(ManfredForegroundHandler());
}

void requireServiceRequestSuccess(ServiceRequestResult result) {
  switch (result) {
    case ServiceRequestSuccess():
      return;
    case ServiceRequestFailure():
      final Object error = result.error;
      throw StateError('Foreground service request failed: $error');
  }
}

class ManfredForegroundHandler extends TaskHandler {
  @override
  Future<void> onStart(DateTime timestamp, TaskStarter starter) async {}

  @override
  void onRepeatEvent(DateTime timestamp) {}

  @override
  Future<void> onDestroy(DateTime timestamp, bool isTimeout) async {}
}

class ForegroundBridgeService {
  void initialize() {
    FlutterForegroundTask.init(
      androidNotificationOptions: AndroidNotificationOptions(
        channelId: 'manfred_companion',
        channelName: 'Manfred Companion',
        channelDescription: 'Keeps the Omi BLE audio bridge connected.',
        onlyAlertOnce: true,
      ),
      iosNotificationOptions: const IOSNotificationOptions(
        showNotification: false,
        playSound: false,
      ),
      foregroundTaskOptions: ForegroundTaskOptions(
        eventAction: ForegroundTaskEventAction.repeat(30000),
        autoRunOnBoot: false,
        autoRunOnMyPackageReplaced: false,
        allowWakeLock: true,
        allowWifiLock: true,
      ),
    );
  }

  Future<void> requestNotificationPermission() async {
    if (await FlutterForegroundTask.checkNotificationPermission() != NotificationPermission.granted) {
      await FlutterForegroundTask.requestNotificationPermission();
    }
  }

  Future<ServiceRequestResult> start() async {
    await requestNotificationPermission();
    if (await FlutterForegroundTask.isRunningService) {
      return FlutterForegroundTask.restartService();
    }
    return FlutterForegroundTask.startService(
      serviceId: 891,
      serviceTypes: const <ForegroundServiceTypes>[
        ForegroundServiceTypes.connectedDevice,
      ],
      notificationTitle: 'Manfred is listening through Omi',
      notificationText: 'Tap to view capture and upload status',
      callback: manfredForegroundCallback,
    );
  }

  Future<ServiceRequestResult> stop() => FlutterForegroundTask.stopService();
}
