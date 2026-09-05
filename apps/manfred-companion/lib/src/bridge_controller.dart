import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:uuid/uuid.dart';

import 'audio_pipeline.dart';
import 'chat_mirror.dart';
import 'chunk_spool.dart';
import 'config_store.dart';
import 'foreground_service.dart';
import 'generation_packet_serial.dart';
import 'lifecycle_coordination.dart';
import 'manfred_uploader.dart';
import 'omi_ble_transport.dart';
import 'omi_protocol.dart';
import 'opus_decoder.dart';
import 'validation_evidence.dart';

class CaptureSessionCounters {
  int packets = 0;
  int decodedPcmBytes = 0;
  int uploadedChunks = 0;

  void reset() {
    packets = 0;
    decodedPcmBytes = 0;
    uploadedChunks = 0;
  }

  void recordPacket() {
    packets++;
  }

  void recordDecoded(int decodedBytes) {
    decodedPcmBytes += decodedBytes;
  }

  void recordUpload({required String chunkSessionId, required String? activeSessionId}) {
    if (chunkSessionId == activeSessionId) {
      uploadedChunks++;
    }
  }
}

class BridgeController extends ChangeNotifier {
  BridgeController({
    ConfigStore? configStore,
    ForegroundBridgeService? foregroundService,
    NativeChatMirrorBridge? nativeChatMirrorBridge,
  })  : _configStore = configStore ?? ConfigStore(),
        _foregroundService = foregroundService ?? ForegroundBridgeService(),
        _nativeChatMirrorBridge =
            nativeChatMirrorBridge ?? MethodChannelNativeChatMirrorBridge();

  final ConfigStore _configStore;
  final ForegroundBridgeService _foregroundService;
  final NativeChatMirrorBridge _nativeChatMirrorBridge;
  CompanionConfig _config = const CompanionConfig(endpoint: '', receiverToken: '');
  ChunkSpool? _spool;
  ChatMirrorSpool? _chatMirrorSpool;
  OmiBleTransport? _ble;
  ManfredUploader? _uploader;
  ChatMirrorUploader? _chatMirrorUploader;
  OmiAudioDecoder? _decoder;
  PcmChunker? _chunker;
  Directory? _supportDirectory;
  ValidationEvidenceArchive? _validationEvidence;
  String? _validationEvidencePath;
  Timer? _retryTimer;
  Timer? _chatMirrorTimer;
  GenerationPacketSerial _packetSerial = GenerationPacketSerial();
  final BackgroundDrainCoordinator _drainCoordinator = BackgroundDrainCoordinator();
  final BridgeStopCoordinator _stopCoordinator = BridgeStopCoordinator();
  final PendingCount _pendingCount = PendingCount();
  final UploadLease _uploadLease = UploadLease();
  Future<void>? _chatMirrorSyncFuture;
  final Set<String> _rejectedChatMirrorEventIds = <String>{};
  bool _chatMirrorDeleteRequested = false;
  bool _running = false;
  String _bleStatus = 'idle';
  String? _lastError;
  String? _sourceSessionId;
  String? _sourceUid;
  OmiCodec? _codec;
  final CaptureSessionCounters _captureCounters = CaptureSessionCounters();
  int _nextPacketSequence = 0;
  int _stalePacketsDropped = 0;
  int _validationWriteErrors = 0;
  int _queuedChatMirrorEvents = 0;
  bool _chatMirrorAccessibilityEnabled = false;
  String _chatMirrorStatus = 'not configured';

  CompanionConfig get config => _config;
  bool get running => _running;
  String get bleStatus => _bleStatus;
  String? get lastError => _lastError;
  OmiCodec? get codec => _codec;
  int get packetsReceived => _captureCounters.packets;
  int get decodedPcmBytes => _captureCounters.decodedPcmBytes;
  int get uploadedChunks => _captureCounters.uploadedChunks;
  int get queuedChunks => _pendingCount.value;
  String? get sourceSessionId => _sourceSessionId;
  String? get validationEvidencePath => _validationEvidencePath;
  int get stalePacketsDropped => _stalePacketsDropped;
  int get validationWriteErrors => _validationWriteErrors;
  int get queuedChatMirrorEvents => _queuedChatMirrorEvents;
  bool get chatMirrorAccessibilityEnabled => _chatMirrorAccessibilityEnabled;
  String get chatMirrorStatus => _chatMirrorStatus;

  Future<void> initialize() async {
    initializeForegroundCommunication();
    _foregroundService.initialize();
    _config = await _configStore.load();
    final Directory support = await getApplicationSupportDirectory();
    _supportDirectory = support;
    final Directory validationRoot = Directory('${support.path}/manfred-validation');
    if (await validationRoot.exists()) {
      _validationEvidencePath = validationRoot.path;
    }
    _spool = ChunkSpool(Directory('${support.path}/manfred-spool'));
    await _spool!.initialize();
    _chatMirrorSpool = ChatMirrorSpool(
      Directory('${support.path}/manfred-chat-mirror-spool'),
    );
    await _chatMirrorSpool!.initialize();
    _configureChatMirrorUploader();
    await _refreshQueued();
    final bool uploaderReady = _ensureUploader();
    if (uploaderReady && _pendingCount.value > 0) {
      _scheduleFreshDrain();
    }
    _startChatMirrorTimer();
    notifyListeners();
    unawaited(syncChatMirror());
  }

  Future<void> saveConfig({
    required String endpoint,
    required String token,
    String? chatMirrorEndpoint,
    String? chatMirrorToken,
    bool? validationCaptureEnabled,
  }) async {
    if (_running) {
      throw StateError('Stop capture before changing Companion settings');
    }
    final String normalizedEndpoint = endpoint.trim();
    final String normalizedToken = token.trim();
    if (normalizedEndpoint.isEmpty != normalizedToken.isEmpty) {
      throw const FormatException(
        'Audio receiver endpoint and token must both be configured or both be blank',
      );
    }
    if (normalizedEndpoint.isNotEmpty) {
      ManfredEndpoint.parse(normalizedEndpoint);
    }
    final String normalizedChatEndpoint =
        (chatMirrorEndpoint ?? _config.chatMirrorEndpoint).trim();
    final String normalizedChatToken =
        (chatMirrorToken ?? _config.chatMirrorToken).trim();
    if (normalizedChatEndpoint.isEmpty != normalizedChatToken.isEmpty) {
      throw const FormatException(
        'Chat Mirror endpoint and token must both be configured or both be blank',
      );
    }
    if (normalizedChatEndpoint.isNotEmpty) {
      ChatMirrorEndpoint.parse(normalizedChatEndpoint);
    }
    final CompanionConfig nextConfig = CompanionConfig(
      endpoint: normalizedEndpoint,
      receiverToken: normalizedToken,
      chatMirrorEndpoint: normalizedChatEndpoint,
      chatMirrorToken: normalizedChatToken,
      deviceId: _config.deviceId,
      validationCaptureEnabled:
          validationCaptureEnabled ?? _config.validationCaptureEnabled,
    );
    await _configStore.save(nextConfig);
    _config = nextConfig;
    _replaceUploader();
    if (_pendingCount.value > 0) {
      _scheduleFreshDrain();
    }
    _configureChatMirrorUploader();
    await syncChatMirror();
    notifyListeners();
  }

  Future<List<OmiScanDevice>> scan() async {
    await _requestBluetoothPermissions();
    _setStatus('scanning');
    final OmiBleTransport scanner = _newBleTransport();
    try {
      return await scanner.scan();
    } finally {
      _setStatus('idle');
    }
  }

  Future<void> selectDevice(OmiScanDevice device) async {
    _config = CompanionConfig(
      endpoint: _config.endpoint,
      receiverToken: _config.receiverToken,
      chatMirrorEndpoint: _config.chatMirrorEndpoint,
      chatMirrorToken: _config.chatMirrorToken,
      deviceId: device.id,
      validationCaptureEnabled: _config.validationCaptureEnabled,
    );
    await _configStore.save(_config);
    notifyListeners();
  }

  Future<void> start() async {
    if (_running) {
      return;
    }
    if (!_config.isComplete || _config.deviceId == null) {
      throw StateError('Save the tailnet endpoint/token and select an Omi device first');
    }
    await _requestBluetoothPermissions();
    final ManfredEndpoint endpoint = ManfredEndpoint.parse(_config.endpoint);
    _captureCounters.reset();
    _sourceSessionId = const Uuid().v4();
    _sourceUid = pseudonymousSourceUid(_config.deviceId!);
    _packetSerial = GenerationPacketSerial();
    _nextPacketSequence = 0;
    _stalePacketsDropped = 0;
    _validationWriteErrors = 0;
    _chunker = PcmChunker();
    _validationEvidence = null;
    if (_config.validationCaptureEnabled) {
      final Directory? support = _supportDirectory;
      if (support == null) {
        throw StateError('Bridge support directory has not been initialized');
      }
      final ValidationEvidenceArchive evidence = ValidationEvidenceArchive(
        root: Directory('${support.path}/manfred-validation'),
        sourceSessionId: _sourceSessionId!,
      );
      await evidence.initialize();
      _validationEvidence = evidence;
      _validationEvidencePath = evidence.sessionDirectory.path;
    }
    if (!_ensureUploader(endpoint: endpoint)) {
      throw StateError('The saved Manfred endpoint is invalid; update it in Settings');
    }
    _ble = _newBleTransport();
    _lastError = null;
    _running = true;
    notifyListeners();
    try {
      requireServiceRequestSuccess(await _foregroundService.start());
      _retryTimer = Timer.periodic(
        const Duration(seconds: 5),
        (_) => unawaited(_drainSpool()),
      );
      await _ble!.connect(_config.deviceId!);
      _scheduleFreshDrain();
    } catch (error) {
      _recordError(error);
      try {
        await stop();
      } catch (_) {
        // Preserve the original startup error.
      }
      rethrow;
    }
  }

  OmiBleTransport _newBleTransport() => OmiBleTransport(
        onAudioPacket: _onAudioPacket,
        onStatus: _setStatus,
        onCodec: _configureCodec,
      );

  Future<void> _configureCodec(int generation, OmiCodec codec) {
    return _packetSerial.transition(generation, () async {
      final OpusPacketDecoder? opus = codec == OmiCodec.opus10ms || codec == OmiCodec.opus20ms
          ? await NativeOpusPacketDecoder.create()
          : null;
      final OmiAudioDecoder nextDecoder = OmiAudioDecoder(codec: codec, opusDecoder: opus);
      try {
        final PcmChunk? partial = _chunker?.flush();
        if (partial != null) {
          await _enqueueChunk(partial);
        }
        await _refreshQueued();
        await _tryRecordDecoderGeneration(
          generation: generation,
          codec: codec,
          occurredAt: DateTime.now().toUtc(),
        );
      } catch (_) {
        nextDecoder.dispose();
        rethrow;
      }
      final OmiAudioDecoder? previous = _decoder;
      _decoder = nextDecoder;
      _codec = codec;
      previous?.dispose();
      notifyListeners();
    });
  }

  void _onAudioPacket(OmiBlePacket packet) {
    final int generation = packet.generation;
    final List<int> rawPacket = packet.notification;
    final int packetSequence = _nextPacketSequence++;
    final DateTime receivedAt = packet.receivedAt;
    final Future<void> processing = _packetSerial.processPacket(
      generation,
      () async {
        String disposition = 'decode-error';
        try {
          final OmiAudioDecoder? decoder = _decoder;
          if (!_running || decoder == null) {
            disposition = 'capture-not-running';
            return;
          }
          _captureCounters.recordPacket();
          final Uint8List pcm = decoder.decodeNotification(rawPacket);
          disposition = 'decoded';
          _captureCounters.recordDecoded(pcm.length);
          final List<PcmChunk> chunks = _chunker!.add(
            pcm,
            receivedAt: receivedAt,
            decoderGeneration: generation,
          );
          for (final PcmChunk chunk in chunks) {
            await _enqueueChunk(chunk);
          }
          unawaited(_drainSpool());
          notifyListeners();
        } catch (_) {
          if (disposition == 'decoded') {
            disposition = 'post-decode-processing-error';
          }
          rethrow;
        } finally {
          await _tryRecordValidationPacket(
            packetSequence: packetSequence,
            generation: generation,
            receivedAt: receivedAt,
            rawPacket: rawPacket,
            disposition: disposition,
          );
        }
      },
      onStale: () async {
        _stalePacketsDropped++;
        await _tryRecordValidationPacket(
          packetSequence: packetSequence,
          generation: generation,
          receivedAt: receivedAt,
          rawPacket: rawPacket,
          disposition: 'stale-generation-discarded',
        );
        notifyListeners();
      },
    );
    unawaited(_observePacketProcessing(processing));
  }

  Future<void> _tryRecordDecoderGeneration({
    required int generation,
    required OmiCodec codec,
    required DateTime occurredAt,
  }) async {
    final ValidationEvidenceArchive? archive = _validationEvidence;
    if (archive == null) {
      return;
    }
    final bool recorded = await archive.tryRecordDecoderGeneration(
      generation: generation,
      codec: codec,
      occurredAt: occurredAt,
    );
    if (!recorded) {
      _validationWriteErrors = archive.writeErrors;
      notifyListeners();
    }
  }

  Future<void> _tryRecordValidationPacket({
    required int packetSequence,
    required int generation,
    required DateTime receivedAt,
    required List<int> rawPacket,
    required String disposition,
  }) async {
    final ValidationEvidenceArchive? archive = _validationEvidence;
    if (archive == null) {
      return;
    }
    final bool recorded = await archive.tryRecordPacket(
      packetSequence: packetSequence,
      generation: generation,
      receivedAt: receivedAt,
      rawPacket: rawPacket,
      disposition: disposition,
    );
    if (!recorded) {
      _validationWriteErrors = archive.writeErrors;
      notifyListeners();
    }
  }

  Future<void> _closeValidationEvidence() async {
    final ValidationEvidenceArchive? archive = _validationEvidence;
    _validationEvidence = null;
    if (archive == null) {
      return;
    }
    try {
      await archive.close();
    } catch (_) {
      _validationWriteErrors++;
      notifyListeners();
    }
  }

  Future<void> _observePacketProcessing(Future<void> processing) async {
    try {
      await processing;
    } catch (error) {
      _recordError(error);
    }
  }

  Future<void> _enqueueChunk(PcmChunk chunk) async {
    final String? sessionId = _sourceSessionId;
    final String? sourceUid = _sourceUid;
    final ChunkSpool? spool = _spool;
    if (sessionId == null || sourceUid == null || spool == null) {
      throw StateError('Capture spool is not initialized');
    }
    await spool.enqueue(sessionId, sourceUid, chunk);
    _pendingCount.recordEnqueue();
  }

  Future<void> _drainSpool() {
    return _drainCoordinator.run(_startDrainPass);
  }

  Future<void> _startDrainPass() {
    final ManfredUploader? uploader = _uploader;
    final ChunkSpool? spool = _spool;
    if (uploader == null || spool == null) {
      return Future<void>.value();
    }
    return _drainSpoolOnce(uploader, spool, _uploadLease.current());
  }

  void _scheduleFreshDrain() {
    _drainCoordinator.scheduleFresh(_startDrainPass);
  }

  Future<void> _drainSpoolOnce(
    ManfredUploader uploader,
    ChunkSpool spool,
    int uploadGeneration,
  ) async {
    try {
      for (final PendingChunk chunk in await spool.pending()) {
        if (!_uploadLease.isCurrent(uploadGeneration) || !identical(_uploader, uploader)) {
          break;
        }
        try {
          await uploader.upload(chunk);
          await spool.remove(chunk);
          _pendingCount.recordRemoval();
          _captureCounters.recordUpload(
            chunkSessionId: chunk.sourceSessionId,
            activeSessionId: _sourceSessionId,
          );
          _lastError = null;
        } catch (error) {
          if (_uploadLease.isCurrent(uploadGeneration) && identical(_uploader, uploader)) {
            _recordError(error);
          }
          break;
        }
      }
    } catch (error) {
      _recordError(error);
    } finally {
      try {
        await _refreshQueued();
      } catch (error) {
        _recordError(error);
      }
      notifyListeners();
    }
  }

  Future<void> stop() async {
    if (!_running) {
      return;
    }
    _retryTimer?.cancel();
    await _stopCoordinator.stop(
      disconnect: () => _ble?.disconnect() ?? Future<void>.value(),
      drainPackets: _packetSerial.drain,
      markNotRunning: () => _running = false,
      commitFinalChunk: () async {
        final PcmChunk? partial = _chunker?.flush();
        if (partial != null) {
          await _enqueueChunk(partial);
        }
      },
      scheduleUpload: () async => _scheduleFreshDrain(),
      closeValidation: _closeValidationEvidence,
      disposeCapture: () {
        _decoder?.dispose();
        _decoder = null;
        _ble = null;
        _chunker = null;
        _codec = null;
      },
      stopForeground: () async {
        requireServiceRequestSuccess(await _foregroundService.stop());
      },
      refreshQueued: _refreshQueued,
      markStopped: () => _setStatus('stopped'),
    );
  }

  Future<void> retryPending() async {
    if (_running || _pendingCount.value == 0) {
      return;
    }
    if (!_config.isComplete) {
      throw StateError('Save the tailnet endpoint and receiver token before uploading');
    }
    if (!_ensureUploader()) {
      throw StateError('The saved Manfred endpoint is invalid; update it in Settings');
    }
    _scheduleFreshDrain();
  }

  Future<void> deletePending() async {
    if (_running) {
      throw StateError('Stop capture before deleting pending audio');
    }
    await _cancelUploads();
    try {
      await _spool!.deleteAll();
      _pendingCount.replace(0);
    } finally {
      _ensureUploader();
      notifyListeners();
    }
  }

  Future<void> deleteValidationEvidence() async {
    if (_running) {
      throw StateError('Stop capture before deleting validation packet evidence');
    }
    final Directory? support = _supportDirectory;
    if (support == null) {
      return;
    }
    final Directory root = Directory('${support.path}/manfred-validation');
    if (await root.exists()) {
      await root.delete(recursive: true);
    }
    _validationEvidencePath = null;
    notifyListeners();
  }

  void _startChatMirrorTimer() {
    _chatMirrorTimer?.cancel();
    _chatMirrorTimer = Timer.periodic(
      const Duration(seconds: 15),
      (_) => unawaited(syncChatMirror()),
    );
  }

  void _configureChatMirrorUploader() {
    _rejectedChatMirrorEventIds.clear();
    _chatMirrorUploader?.close();
    _chatMirrorUploader = null;
    if (_config.isChatMirrorComplete) {
      _chatMirrorUploader = ChatMirrorUploader(
        endpoint: ChatMirrorEndpoint.parse(_config.chatMirrorEndpoint),
        receiverToken: _config.chatMirrorToken,
      );
      _chatMirrorStatus = 'configured';
    } else {
      _chatMirrorStatus = 'not configured';
    }
  }

  Future<void> openChatMirrorAccessibilitySettings() async {
    await _nativeChatMirrorBridge.openAccessibilitySettings();
  }

  Future<void> syncChatMirror() {
    final Future<void>? current = _chatMirrorSyncFuture;
    if (current != null) {
      return current;
    }
    late final Future<void> operation;
    operation = _syncChatMirrorOnce().whenComplete(() {
      if (identical(_chatMirrorSyncFuture, operation)) {
        _chatMirrorSyncFuture = null;
      }
    });
    _chatMirrorSyncFuture = operation;
    return operation;
  }

  Future<void> _syncChatMirrorOnce() async {
    final ChatMirrorSpool? spool = _chatMirrorSpool;
    if (spool == null) {
      return;
    }
    try {
      _chatMirrorAccessibilityEnabled = await _nativeChatMirrorBridge.isEnabled();
      final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
        nativeBridge: _nativeChatMirrorBridge,
        spool: spool,
      );
      final int imported = await importer.importOnce();
      final ChatMirrorUploader? uploader = _chatMirrorUploader;
      ChatMirrorDrainResult drainResult = const ChatMirrorDrainResult(
        uploaded: 0,
        newlyRejected: 0,
        retainedRejected: 0,
      );
      if (uploader != null) {
        drainResult = await drainChatMirrorSpool(
          spool: spool,
          uploader: uploader,
          rejectedEventIds: _rejectedChatMirrorEventIds,
          shouldStop: () => _chatMirrorDeleteRequested,
        );
      }
      _queuedChatMirrorEvents = (await spool.pending()).length;
      final String rejectionSuffix = drainResult.retainedRejected > 0
          ? '; ${drainResult.retainedRejected} rejected retained'
          : '';
      _chatMirrorStatus = uploader == null
          ? (_chatMirrorAccessibilityEnabled
              ? 'capturing locally; receiver not configured'
              : 'accessibility disabled; receiver not configured')
          : (_chatMirrorAccessibilityEnabled
              ? 'active${imported > 0 ? '; imported $imported' : ''}$rejectionSuffix'
              : 'receiver ready; accessibility disabled$rejectionSuffix');
    } on MissingPluginException {
      _chatMirrorStatus = 'native Accessibility bridge unavailable';
    } catch (error) {
      _chatMirrorStatus = 'error: $error';
      _recordError(error);
    } finally {
      notifyListeners();
    }
  }

  Future<void> deletePendingChatMirror() async {
    final ChatMirrorSpool? spool = _chatMirrorSpool;
    if (spool == null) {
      return;
    }
    _chatMirrorDeleteRequested = true;
    _chatMirrorTimer?.cancel();
    _chatMirrorTimer = null;
    final ChatMirrorUploader? activeUploader = _chatMirrorUploader;
    _chatMirrorUploader = null;
    activeUploader?.close();
    bool deleted = false;
    try {
      await _chatMirrorSyncFuture;
      final ChatMirrorImportCoordinator importer = ChatMirrorImportCoordinator(
        nativeBridge: _nativeChatMirrorBridge,
        spool: spool,
      );
      await importer.deleteNativePending();
      await spool.deleteAll();
      _rejectedChatMirrorEventIds.clear();
      _queuedChatMirrorEvents = 0;
      deleted = true;
    } finally {
      _chatMirrorDeleteRequested = false;
      _configureChatMirrorUploader();
      if (deleted) {
        _chatMirrorStatus = _chatMirrorAccessibilityEnabled
            ? 'active; queue deleted'
            : 'accessibility disabled; queue deleted';
      }
      _startChatMirrorTimer();
      notifyListeners();
    }
  }

  Future<void> _refreshQueued() async {
    final int revision = _pendingCount.beginReconciliation();
    final int actual = (await _spool?.pending() ?? const <PendingChunk>[]).length;
    _pendingCount.reconcile(revision, actual);
  }

  bool _ensureUploader({ManfredEndpoint? endpoint}) {
    if (_uploader != null || !_config.isComplete) {
      return _uploader != null;
    }
    try {
      _uploader = ManfredUploader(
        endpoint: endpoint ?? ManfredEndpoint.parse(_config.endpoint),
        receiverToken: _config.receiverToken,
      );
      return true;
    } on FormatException catch (error) {
      _lastError =
          'Saved upload endpoint needs repair in Settings; queued audio was preserved. $error';
      return false;
    }
  }

  void _replaceUploader() {
    final ManfredUploader? previous = _uploader;
    _uploadLease.invalidate();
    _uploader = null;
    final bool replacementReady = _ensureUploader();
    if (replacementReady) {
      _lastError = null;
    }
    // Closing immediately aborts any request already owned by superseded
    // settings. The lease check prevents that pass from sending another chunk;
    // the scheduled fresh tail uses only the replacement uploader.
    previous?.close();
  }

  Future<void> _cancelUploads() async {
    _uploadLease.invalidate();
    final ManfredUploader? uploader = _uploader;
    _uploader = null;
    uploader?.close();
    await _drainCoordinator.whenSettled();
  }

  Future<void> _requestBluetoothPermissions() async {
    final Map<Permission, PermissionStatus> result = await <Permission>[
      Permission.bluetoothScan,
      Permission.bluetoothConnect,
      Permission.notification,
    ].request();
    final bool denied = result.entries.any(
      (MapEntry<Permission, PermissionStatus> entry) =>
          entry.key != Permission.notification && !entry.value.isGranted,
    );
    if (denied) {
      throw StateError('Bluetooth scan/connect permission is required');
    }
  }

  void _setStatus(String value) {
    _bleStatus = value;
    notifyListeners();
  }

  void _recordError(Object error) {
    _lastError = error.toString();
    notifyListeners();
  }

  @override
  void dispose() {
    _retryTimer?.cancel();
    _chatMirrorTimer?.cancel();
    _decoder?.dispose();
    _uploadLease.invalidate();
    _uploader?.close();
    _chatMirrorUploader?.close();
    super.dispose();
  }
}
