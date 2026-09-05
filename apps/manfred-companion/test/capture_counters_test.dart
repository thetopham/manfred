import 'package:flutter_test/flutter_test.dart';
import 'package:manfred_companion/src/bridge_controller.dart';

void main() {
  test('capture counters reset and ignore backlog uploads from older sessions', () {
    final CaptureSessionCounters counters = CaptureSessionCounters();
    counters.recordPacket();
    counters.recordDecoded(320);
    counters.recordPacket();
    counters.recordDecoded(640);
    counters.recordUpload(
      chunkSessionId: 'older-capture',
      activeSessionId: 'current-capture',
    );
    counters.recordUpload(
      chunkSessionId: 'current-capture',
      activeSessionId: 'current-capture',
    );

    expect(counters.packets, 2);
    expect(counters.decodedPcmBytes, 960);
    expect(counters.uploadedChunks, 1);

    counters.reset();
    expect(counters.packets, 0);
    expect(counters.decodedPcmBytes, 0);
    expect(counters.uploadedChunks, 0);
  });
}
