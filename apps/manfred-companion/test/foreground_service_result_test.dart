import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/foreground_service.dart';

void main() {
  test('foreground service request success is accepted', () {
    expect(
      () => requireServiceRequestSuccess(const ServiceRequestSuccess()),
      returnsNormally,
    );
  });

  test('foreground service request failure aborts startup', () {
    expect(
      () => requireServiceRequestSuccess(
        const ServiceRequestFailure(error: 'permission denied'),
      ),
      throwsA(isA<StateError>()),
    );
  });
}
