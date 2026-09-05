import 'dart:typed_data';

const String omiServiceUuid = '19b10000-e8f2-537e-4f6c-d104768a1214';
const String omiAudioUuid = '19b10001-e8f2-537e-4f6c-d104768a1214';
const String omiCodecUuid = '19b10002-e8f2-537e-4f6c-d104768a1214';
const int omiPacketHeaderBytes = 3;
const int omiPcmSampleRate = 16000;
const int omiPcmChannels = 1;

enum OmiCodec {
  pcm16(0),
  pcm8(1),
  opus10ms(20),
  opus20ms(21);

  const OmiCodec(this.id);
  final int id;

  static OmiCodec fromCharacteristic(List<int> value) {
    if (value.isEmpty) {
      throw const FormatException('Omi codec characteristic was empty');
    }
    return OmiCodec.values.firstWhere(
      (codec) => codec.id == value.first,
      orElse: () => throw FormatException('Unsupported Omi codec id ${value.first}'),
    );
  }
}

Uint8List stripOmiHeader(List<int> packet) {
  if (packet.length <= omiPacketHeaderBytes) {
    throw const FormatException('Omi audio packet is shorter than its 3-byte header');
  }
  return Uint8List.fromList(packet.sublist(omiPacketHeaderBytes));
}
