import 'dart:typed_data';

import 'package:opus_codec_dart/opus_codec_dart.dart';
import 'package:opus_codec_platform_interface/opus_codec_platform_interface.dart';

import 'audio_pipeline.dart';
import 'omi_protocol.dart';

class NativeOpusPacketDecoder implements OpusPacketDecoder {
  NativeOpusPacketDecoder._(this._decoder);

  final SimpleOpusDecoder _decoder;
  static bool _initialized = false;

  static Future<NativeOpusPacketDecoder> create() async {
    if (!_initialized) {
      initOpus(await OpusFlutterPlatform.instance.load());
      _initialized = true;
    }
    return NativeOpusPacketDecoder._(
      SimpleOpusDecoder(sampleRate: omiPcmSampleRate, channels: omiPcmChannels),
    );
  }

  @override
  Int16List decode(Uint8List packet) => _decoder.decode(input: packet);

  @override
  void dispose() => _decoder.destroy();
}
