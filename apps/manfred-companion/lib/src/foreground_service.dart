import 'package:flutter_foreground_task/flutter_foreground_task.dart';

import 'foreground_owners.dart';

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
  late final ForegroundOwners _owners = ForegroundOwners(
    startService: () async => requireServiceRequestSuccess(await _startService()),
    stopService: () async => requireServiceRequestSuccess(await FlutterForegroundTask.stopService()),
  );

  Future<void> acquire(String owner) => _owners.acquire(owner);
  Future<void> release(String owner) => _owners.release(owner);

  Future<ServiceRequestResult> _request(Future<void> Function() operation) async {
    try {
      await operation();
      return const ServiceRequestSuccess();
    } catch (error) {
      return ServiceRequestFailure(error: error);
    }
  }

  Future<ServiceRequestResult> start() => _request(() => acquire('ears'));
  Future<ServiceRequestResult> stop() => _request(() => release('ears'));

  void initialize() {
    FlutterForegroundTask.init(
      androidNotificationOptions: AndroidNotificationOptions(
        channelId: 'manfred_companion',
        channelName: 'Manfred Companion',
        channelDescription: 'Keeps active wearable sessions connected.',
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

  Future<ServiceRequestResult> _startService() async {
    await requestNotificationPermission();
    if (await FlutterForegroundTask.isRunningService) {
      return FlutterForegroundTask.restartService();
    }
    return FlutterForegroundTask.startService(
      serviceId: 891,
      serviceTypes: const <ForegroundServiceTypes>[
        ForegroundServiceTypes.connectedDevice,
      ],
      notificationTitle: 'Manfred wearable session active',
      notificationText: 'Tap to view audio and photo sessions',
      callback: manfredForegroundCallback,
    );
  }

}
