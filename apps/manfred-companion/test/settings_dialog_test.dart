import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/main.dart';
import 'package:manfred_companion/src/bridge_controller.dart';
import 'package:manfred_companion/src/config_store.dart';

class MemoryConfigStore extends ConfigStore {
  MemoryConfigStore(this.value);

  CompanionConfig value;

  @override
  Future<CompanionConfig> load() async => value;

  @override
  Future<void> save(CompanionConfig config) async {
    value = config;
  }
}

class AlwaysRunningBridgeController extends BridgeController {
  AlwaysRunningBridgeController(ConfigStore store) : super(configStore: store);

  @override
  bool get running => true;
}

void main() {
  testWidgets('settings are locked while capture is active', (WidgetTester tester) async {
    final MemoryConfigStore store = MemoryConfigStore(
      const CompanionConfig(endpoint: '', receiverToken: ''),
    );
    final AlwaysRunningBridgeController controller = AlwaysRunningBridgeController(store);

    await tester.pumpWidget(ManfredCompanionApp(controller: controller));

    final IconButton settings = tester.widget<IconButton>(
      find.widgetWithIcon(IconButton, Icons.settings),
    );
    expect(settings.onPressed, isNull);

    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });

  testWidgets('settings close cleanly and keep an existing saved token', (
    WidgetTester tester,
  ) async {
    final MemoryConfigStore store = MemoryConfigStore(
      const CompanionConfig(endpoint: '', receiverToken: ''),
    );
    final BridgeController controller = BridgeController(configStore: store);
    await controller.saveConfig(
      endpoint: 'http://100.64.0.10:8787/audio',
      token: 'saved-token',
    );

    await tester.pumpWidget(ManfredCompanionApp(controller: controller));
    await tester.tap(find.byIcon(Icons.settings));
    await tester.pumpAndSettle();

    expect(find.byType(SettingsDialog), findsOneWidget);
    expect(find.text('Saved securely — leave blank to keep it'), findsOneWidget);
    expect(
      tester.widget<TextField>(find.byKey(const Key('settings-endpoint'))).controller!.text,
      'http://100.64.0.10:8787/audio',
    );
    expect(
      tester.widget<TextField>(find.byKey(const Key('settings-token'))).controller!.text,
      isEmpty,
    );
    expect(
      tester.widget<CheckboxListTile>(find.byKey(const Key('settings-validation-capture'))).value,
      isFalse,
    );

    await tester.enterText(
      find.byKey(const Key('settings-endpoint')),
      '  http://100.64.0.11:8787/audio  ',
    );
    await tester.tap(find.byKey(const Key('settings-validation-capture')));
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.byType(SettingsDialog), findsNothing);
    expect(store.value.endpoint, 'http://100.64.0.11:8787/audio');
    expect(store.value.receiverToken, 'saved-token');
    expect(store.value.validationCaptureEnabled, isTrue);

    await tester.tap(find.byIcon(Icons.settings));
    await tester.pumpAndSettle();
    expect(
      tester.widget<TextField>(find.byKey(const Key('settings-endpoint'))).controller!.text,
      'http://100.64.0.11:8787/audio',
    );
    await tester.enterText(
      find.byKey(const Key('settings-token')),
      'replacement-token',
    );
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(store.value.receiverToken, 'replacement-token');

    await tester.tap(find.byIcon(Icons.settings));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const Key('settings-endpoint')),
      'http://100.64.0.99:8787/audio',
    );
    await tester.enterText(
      find.byKey(const Key('settings-token')),
      'discarded-token',
    );
    await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(find.byType(SettingsDialog), findsNothing);
    expect(store.value.endpoint, 'http://100.64.0.11:8787/audio');
    expect(store.value.receiverToken, 'replacement-token');

    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });
}
