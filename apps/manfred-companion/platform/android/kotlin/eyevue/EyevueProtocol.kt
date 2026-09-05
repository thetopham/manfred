// Ported from thetopham/Alternative-HeyCyan-App-and-SDK; see SOURCE_PROVENANCE.md.
package com.thetopham.manfred_companion.eyevue

import java.io.ByteArrayOutputStream
import java.io.StringReader
import java.nio.charset.StandardCharsets
import java.util.Calendar
import java.util.UUID

data class EyevueFrame(
    val commandId: Int,
    val payload: ByteArray,
)

data class EyevueBattery(
    val percent: Int,
    val isCharging: Boolean,
)

data class EyevueCustomer(
    val project: String,
    val customer: String,
)

data class EyevueDeviceInfo(
    val btVersion: String,
    val ispVersion: String,
    val deviceVersion: String,
)

data class EyevueVoiceAssistantStatus(
    val localOfflineSpeechEnabled: Boolean,
    val aiWakeWordEnabled: Boolean,
)

/**
 * Encapsulates the Eyevue smart glasses protocol based on reverse-engineered sources.
 *
 * Eyevue BLE datagram format:
 * [sof_hi, 0x55, len_hi, len_lo, commandId, payload..., crc]
 * Commands use 0xAB; AA14 responses use 0xAC. Legacy 0xAB input remains accepted.
 * Where:
 * - len = payload.size + 2 (command plus CRC, 2 bytes big-endian)
 * - crc = (commandId + sum(payload)) & 0xFF
 */
object EyevueProtocol {
    const val SOF_HI = 0xAB.toByte()
    const val RESPONSE_SOF_HI = 0xAC.toByte()
    const val SOF_LO = 0x55.toByte()

    val SERVICE_UUID: UUID = UUID.fromString("0000aa12-0000-1000-8000-00805f9b34fb")
    val COMMAND_WRITE_UUID: UUID = UUID.fromString("0000aa13-0000-1000-8000-00805f9b34fb")
    val COMMAND_NOTIFY_UUID: UUID = UUID.fromString("0000aa14-0000-1000-8000-00805f9b34fb")
    val PHOTO_NOTIFY_UUID: UUID = UUID.fromString("0000aa15-0000-1000-8000-00805f9b34fb")
    val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    const val CMD_BRIGHTNESS = 1
    const val CMD_RECORD_TIME = 2
    const val CMD_WEAR_DETECT = 4
    const val CMD_GET_CAPACITY = 22
    const val CMD_GET_BATTERY = 23
    const val CMD_TAKE_PHOTO = 34
    const val CMD_RECORD_VIDEO = 35
    const val CMD_STOP_RECORD = 36
    const val CMD_CONTROL_MUSIC = 49
    const val CMD_CONTROL_VOLUME = 50
    const val CMD_RECORD_AUDIO = 52
    const val CMD_PULL_IMAGE = 54
    const val CMD_GET_WIFI_INFO = 57
    const val CMD_GET_CUSTOMER = 100
    const val CMD_APP_LIVE = 103
    const val CMD_GET_DEVICE_STATUS = 72
    const val CMD_GET_DEVICE_INFO = 85
    const val CMD_GET_VOLUME = 105
    const val CMD_GET_MEDIA_COUNT = 64
    const val CMD_SET_TIME = 89
    const val CMD_FILE_DOWNLOAD_FINISH = 68
    const val CMD_GET_SUPPORT_FUNCTION = 149
    const val CMD_STOP_VOICE_RECOGNITION = 86
    const val CMD_GET_VOICE_ASSISTANT_STATUS = 113
    const val CMD_SET_VOICE_ASSISTANT_STATUS = 114

    const val CMD_RECEIVE_BATTERY = 83
    const val CMD_RECEIVE_WIFI_INFO = 37
    const val CMD_RECEIVE_THUMBNAIL_COUNT = 66
    const val CMD_RECEIVE_CUSTOMER = 100
    const val CMD_RECEIVE_VOICE_DATA = 70
    const val CMD_RECEIVE_VOICE_DATA_START = 151
    const val CMD_RECEIVE_VOICE_DATA_END = 153
    const val CMD_RECEIVE_PHOTO_DATA_START = 151
    const val CMD_RECEIVE_PHOTO_DATA = 152
    const val CMD_RECEIVE_PHOTO_DATA_END = 153

    const val CMD_START_LIVE = CMD_APP_LIVE

    const val PARAM_LIVE_AP = 0x30.toByte()    // '0'
    const val PARAM_LIVE_P2P = 0x31.toByte()   // '1'
    const val PARAM_DOWNLOAD_FINISH = 0x30.toByte()
    const val PARAM_PHOTO_THUMBNAIL = 0x30
    const val PARAM_PHOTO_HIGH_QUALITY = 0x31
    const val PARAM_RECORD_START = 0x01
    const val PARAM_RECORD_STOP = 0x00
    const val PARAM_AUDIO_START = 0x01
    const val PARAM_AUDIO_STOP = 0x00
    const val PARAM_WIFI_AP = 0x30
    const val PARAM_WIFI_P2P = 0x31

    /**
     * Builds a full Eyevue BLE datagram packet.
     */
    fun buildDatagram(commandId: Int, payload: ByteArray = byteArrayOf()): ByteArray {
        require(commandId in 0..0xFF) { "Eyevue command must fit in one byte" }
        require(payload.size <= 0xFFFD) { "Eyevue payload is too large" }
        val len = payload.size + 2
        val packet = ByteArray(5 + payload.size + 1)
        packet[0] = SOF_HI
        packet[1] = SOF_LO
        packet[2] = ((len shr 8) and 0xFF).toByte()
        packet[3] = (len and 0xFF).toByte()
        packet[4] = (commandId and 0xFF).toByte()

        var crc = commandId and 0xFF
        for (i in payload.indices) {
            packet[5 + i] = payload[i]
            crc += (payload[i].toInt() and 0xFF)
        }
        packet[packet.size - 1] = (crc and 0xFF).toByte()
        return packet
    }

    internal fun isDatagramHeader(high: Byte, low: Byte): Boolean =
        (high == SOF_HI || high == RESPONSE_SOF_HI) && low == SOF_LO

    fun parseDatagram(packet: ByteArray): EyevueFrame {
        require(packet.size >= 6) { "Eyevue packet is too short" }
        require(isDatagramHeader(packet[0], packet[1])) { "Invalid Eyevue packet header" }
        val declaredLength = u16(packet[2], packet[3])
        require(declaredLength >= 2) { "Invalid Eyevue packet length: $declaredLength" }
        require(packet.size == declaredLength + 4) {
            "Eyevue packet length mismatch: declared=$declaredLength actual=${packet.size - 4}"
        }
        val commandId = packet[4].toInt() and 0xFF
        val payload = packet.copyOfRange(5, packet.size - 1)
        val expectedCrc = crc(commandId, payload)
        val actualCrc = packet.last().toInt() and 0xFF
        require(actualCrc == expectedCrc) {
            "Eyevue CRC mismatch: expected=$expectedCrc actual=$actualCrc"
        }
        return EyevueFrame(commandId, payload)
    }

    fun crc(commandId: Int, payload: ByteArray): Int {
        var result = commandId and 0xFF
        payload.forEach { result += it.toInt() and 0xFF }
        return result and 0xFF
    }

    fun valuePacket(commandId: Int, value: Int): ByteArray =
        buildDatagram(commandId, byteArrayOf((value and 0xFF).toByte()))

    fun twoValuePacket(commandId: Int, first: Int, second: Int): ByteArray =
        buildDatagram(
            commandId,
            byteArrayOf((first and 0xFF).toByte(), (second and 0xFF).toByte()),
        )

    fun buildGetCustomerPacket(): ByteArray = buildDatagram(CMD_GET_CUSTOMER)

    fun buildGetBatteryPacket(): ByteArray = valuePacket(CMD_GET_BATTERY, 0)

    fun buildGetCapacityPacket(): ByteArray = valuePacket(CMD_GET_CAPACITY, 0)

    fun buildGetDeviceStatusPacket(): ByteArray = valuePacket(CMD_GET_DEVICE_STATUS, 0)

    fun buildGetDeviceInfoPacket(): ByteArray = valuePacket(CMD_GET_DEVICE_INFO, 0)

    fun buildGetWifiInfoPacket(p2p: Boolean): ByteArray =
        valuePacket(CMD_GET_WIFI_INFO, if (p2p) PARAM_WIFI_P2P else PARAM_WIFI_AP)

    fun buildPhotoPacket(highQuality: Boolean): ByteArray =
        valuePacket(CMD_TAKE_PHOTO, if (highQuality) PARAM_PHOTO_HIGH_QUALITY else PARAM_PHOTO_THUMBNAIL)

    /**
     * Vendor appPullImage(type): the single byte's index/quality meaning is unconfirmed.
     * Keep it opaque for controlled probes; this does not imply a high-resolution mode.
     */
    fun buildPullImagePacket(type: Int): ByteArray {
        require(type in 0..0xFF) { "Eyevue image pull type must fit in one byte" }
        return valuePacket(CMD_PULL_IMAGE, type)
    }

    fun buildStartVideoPacket(): ByteArray = valuePacket(CMD_RECORD_VIDEO, PARAM_RECORD_START)

    fun buildStopVideoPacket(): ByteArray = valuePacket(CMD_STOP_RECORD, PARAM_RECORD_STOP)

    fun buildAudioPacket(start: Boolean): ByteArray =
        valuePacket(CMD_RECORD_AUDIO, if (start) PARAM_AUDIO_START else PARAM_AUDIO_STOP)

    fun buildFinishTransferPacket(): ByteArray =
        buildDatagram(CMD_FILE_DOWNLOAD_FINISH, byteArrayOf(PARAM_DOWNLOAD_FINISH, 0x01))

    fun buildWearDetectionPacket(enabled: Boolean): ByteArray =
        valuePacket(CMD_WEAR_DETECT, if (enabled) 0x31 else 0x30)

    fun buildStopVoiceRecognitionPacket(): ByteArray =
        valuePacket(CMD_STOP_VOICE_RECOGNITION, 0)

    fun buildGetVoiceAssistantStatusPacket(): ByteArray =
        valuePacket(CMD_GET_VOICE_ASSISTANT_STATUS, 0)

    fun buildSetVoiceAssistantStatusPacket(
        localOfflineSpeechEnabled: Boolean,
        aiWakeWordEnabled: Boolean,
    ): ByteArray {
        var value = if (localOfflineSpeechEnabled) 0 else 1
        if (!aiWakeWordEnabled) value = value or 2
        return valuePacket(CMD_SET_VOICE_ASSISTANT_STATUS, value)
    }

    fun buildRecordDurationPacket(seconds: Int): ByteArray =
        twoValuePacket(CMD_RECORD_TIME, seconds shr 8, seconds)

    fun buildSetTimePacket(epochMillis: Long = System.currentTimeMillis()): ByteArray {
        val calendar = Calendar.getInstance().apply { timeInMillis = epochMillis }
        return buildDatagram(
            CMD_SET_TIME,
            byteArrayOf(
                (calendar.get(Calendar.YEAR) - 2000).toByte(),
                (calendar.get(Calendar.MONTH) + 1).toByte(),
                calendar.get(Calendar.DAY_OF_MONTH).toByte(),
                calendar.get(Calendar.HOUR_OF_DAY).toByte(),
                calendar.get(Calendar.MINUTE).toByte(),
                calendar.get(Calendar.SECOND).toByte(),
            ),
        )
    }

    fun parseBattery(frame: EyevueFrame): EyevueBattery? {
        if (frame.payload.size < 2) return null
        val battery = when (frame.commandId) {
            CMD_GET_BATTERY -> {
                // Vendor 0x17 response encodes decimal digits in the low nibbles.
                val tens = frame.payload[0].toInt() and 0x0F
                val units = frame.payload[1].toInt() and 0x0F
                if (tens > 10 || units > 9 || (tens == 10 && units != 0)) return null
                val charging = frame.payload.getOrNull(2)?.toInt()?.and(0xFF) ?: 0
                if (charging !in 0..1) return null
                EyevueBattery(tens * 10 + units, charging == 1)
            }
            CMD_RECEIVE_BATTERY -> {
                // TK8 0x53 push: charging flag followed by an unsigned percentage.
                val charging = frame.payload[0].toInt() and 0xFF
                if (charging !in 0..1) return null
                EyevueBattery(frame.payload[1].toInt() and 0xFF, charging == 1)
            }
            else -> return null
        }
        return battery.takeIf { it.percent in 0..100 }
    }

    fun parseWifiSsid(frame: EyevueFrame): String? {
        if (frame.commandId != CMD_RECEIVE_WIFI_INFO || frame.payload.isEmpty()) return null
        return frame.payload.toString(StandardCharsets.UTF_8)
            .trim { it <= ' ' || it == '\u0000' }
            .takeIf { it.isNotBlank() }
    }

    fun parseCustomer(frame: EyevueFrame): EyevueCustomer? {
        if (frame.commandId != CMD_RECEIVE_CUSTOMER || frame.payload.size < 8) return null
        fun readPart(offset: Int): String = frame.payload
            .copyOfRange(offset, offset + 4)
            .filter { it.toInt() != 0 }
            .toByteArray()
            .toString(StandardCharsets.UTF_8)
        return EyevueCustomer(
            project = readPart(0),
            customer = readPart(4),
        )
    }


    /** Vendor 0x55 reply: three BT bytes, three ISP bytes, then one device-version byte. */
    fun parseDeviceInfo(frame: EyevueFrame): EyevueDeviceInfo? {
        if (frame.commandId != CMD_GET_DEVICE_INFO || frame.payload.size < 7) return null
        fun version(offset: Int): String =
            (offset until offset + 3).joinToString(".") { (frame.payload[it].toInt() and 0xff).toString() }
        return EyevueDeviceInfo(
            btVersion = version(0),
            ispVersion = version(3),
            deviceVersion = (frame.payload[6].toInt() and 0xff).toString(),
        )
    }

    fun parseVoiceAssistantStatus(frame: EyevueFrame): EyevueVoiceAssistantStatus? {
        if (frame.commandId != CMD_GET_VOICE_ASSISTANT_STATUS || frame.payload.isEmpty()) return null
        val value = frame.payload[0].toInt() and 0xFF
        return EyevueVoiceAssistantStatus(
            localOfflineSpeechEnabled = value and 1 == 0,
            aiWakeWordEnabled = value and 2 == 0,
        )
    }

    private fun u16(high: Byte, low: Byte): Int =
        ((high.toInt() and 0xFF) shl 8) or (low.toInt() and 0xFF)

    /** Start Live streaming over AP mode. */
    fun buildStartLiveApPacket(): ByteArray = buildDatagram(CMD_APP_LIVE, byteArrayOf(PARAM_LIVE_AP))

    /** Start Live streaming over P2P mode. */
    fun buildStartLiveP2pPacket(): ByteArray = buildDatagram(CMD_APP_LIVE, byteArrayOf(PARAM_LIVE_P2P))

    /** Stop live stream / complete file transfer. */
    fun buildExitLivePacket(): ByteArray = buildFinishTransferPacket()

    /** Trigger photo snapshot. */
    fun buildTakePhotoPacket(): ByteArray = buildPhotoPacket(highQuality = true)

    /** Start video recording. */
    fun buildRecordVideoPacket(): ByteArray = buildStartVideoPacket()

    /** Stop video recording. */
    fun buildStopRecordPacket(): ByteArray = buildStopVideoPacket()

    /** Returns the primary HTTP index URL for Eyevue glasses (SK series P2P XML index). */
    fun eyevueMediaIndexUrl(deviceIp: String): String = "http://$deviceIp/?custom=1&cmd=3001&par=1"

    /**
     * Parses filenames (JPG, MP4, OPUS) from Eyevue XML responses or plain text fallback.
     */
    fun parseMediaListFromXml(content: String): List<String> {
        val fileNames = mutableListOf<String>()
        try {
            val factory = org.xmlpull.v1.XmlPullParserFactory.newInstance()
            factory.isNamespaceAware = false
            val parser = factory.newPullParser()
            parser.setInput(java.io.StringReader(content))
            var eventType = parser.eventType
            var currentTag = ""
            while (eventType != org.xmlpull.v1.XmlPullParser.END_DOCUMENT) {
                when (eventType) {
                    org.xmlpull.v1.XmlPullParser.START_TAG -> currentTag = parser.name
                    org.xmlpull.v1.XmlPullParser.TEXT -> {
                        if (currentTag == "NAME" || currentTag == "FPATH") {
                            val text = parser.text.trim()
                            if (text.isNotBlank() && (
                                    text.endsWith(".jpg", ignoreCase = true) ||
                                        text.endsWith(".jpeg", ignoreCase = true) ||
                                        text.endsWith(".mp4", ignoreCase = true) ||
                                        text.endsWith(".opus", ignoreCase = true)
                                )) {
                                val fileName = text.substringAfterLast('\\').substringAfterLast('/')
                                if (fileName !in fileNames) fileNames.add(fileName)
                            }
                        }
                    }
                }
                eventType = parser.next()
            }
        } catch (_: Exception) {
            // Fallback line parsing for filenames in plaintext/config format
            content.lineSequence()
                .map { it.trim() }
                .filter { it.endsWith(".jpg", ignoreCase = true) || it.endsWith(".mp4", ignoreCase = true) || it.endsWith(".opus", ignoreCase = true) }
                .forEach { if (it !in fileNames) fileNames.add(it) }
        }
        return fileNames
    }
}

/** Buffers BLE notifications so frames split across ATT packets are decoded safely. */
class EyevueFrameDecoder {
    private val buffer = ByteArrayOutputStream()

    fun append(chunk: ByteArray): List<EyevueFrame> {
        if (chunk.isEmpty()) return emptyList()
        buffer.write(chunk)
        val bytes = buffer.toByteArray()
        val frames = mutableListOf<EyevueFrame>()
        var cursor = 0

        while (true) {
            while (cursor + 1 < bytes.size &&
                !EyevueProtocol.isDatagramHeader(bytes[cursor], bytes[cursor + 1])
            ) {
                cursor++
            }
            if (bytes.size - cursor < 4) break

            val length = ((bytes[cursor + 2].toInt() and 0xFF) shl 8) or
                (bytes[cursor + 3].toInt() and 0xFF)
            if (length < 2) {
                cursor++
                continue
            }
            val frameSize = length + 4
            if (bytes.size - cursor < frameSize) break

            val candidate = bytes.copyOfRange(cursor, cursor + frameSize)
            runCatching { EyevueProtocol.parseDatagram(candidate) }
                .onSuccess(frames::add)
            cursor += frameSize
        }

        buffer.reset()
        if (cursor < bytes.size) buffer.write(bytes, cursor, bytes.size - cursor)
        return frames
    }

    fun reset() = buffer.reset()
}

/** Reassembles the AA15 photo stream used by the vendor's AI-photo path. */
class EyevuePhotoAssembler(
    private val onTransferRejected: (String) -> Unit = {},
) {
    private companion object {
        const val DATA_OFFSET_START = 5
        const val IMAGE_BYTES_START = 9
        const val MAX_TRANSFER_BYTES = 32 * 1024 * 1024
        const val MAX_TRANSFER_CHUNKS = 65_536
        const val UINT32_MASK = 0xFFFF_FFFFL
    }

    private data class PhotoChunk(
        val rawOffset: Long,
        val bytes: ByteArray,
    )

    private data class RelativeChunk(
        val offset: Int,
        val bytes: ByteArray,
    )

    private val chunks = mutableListOf<PhotoChunk>()
    private var receiving = false
    private var invalidReason: String? = null
    private var announcedBytes: Int? = null
    private var storedChunkBytes = 0

    @Synchronized
    fun append(packet: ByteArray): ByteArray? {
        if (packet.size < 8) return null
        val commandId = packet[4].toInt() and 0xFF
        val payloadEnd = packet.size - 3
        if (payloadEnd < DATA_OFFSET_START) return null

        return when (commandId) {
            EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START -> {
                resetTransfer()
                receiving = true
                if (payloadEnd >= IMAGE_BYTES_START) {
                    // S25 traces announce the wire byte count (including word padding)
                    // as a four-byte big-endian value, followed by a format byte.
                    val size = readUnsigned32(packet, DATA_OFFSET_START)
                    if (size !in 1L..MAX_TRANSFER_BYTES.toLong()) {
                        invalidReason = "invalid_announced_size value=" + size
                    } else {
                        announcedBytes = size.toInt()
                    }
                }
                null
            }

            EyevueProtocol.CMD_RECEIVE_PHOTO_DATA -> {
                if (!receiving || payloadEnd <= IMAGE_BYTES_START) return null
                if (invalidReason != null) return null
                val rawOffset = readUnsigned32(packet, DATA_OFFSET_START)
                val imageBytes = packet.copyOfRange(IMAGE_BYTES_START, payloadEnd)
                val exactDuplicate = chunks.any { chunk ->
                    chunk.rawOffset == rawOffset && chunk.bytes.contentEquals(imageBytes)
                }
                if (!exactDuplicate) {
                    if (
                        chunks.size >= MAX_TRANSFER_CHUNKS ||
                        storedChunkBytes.toLong() + imageBytes.size > MAX_TRANSFER_BYTES
                    ) {
                        invalidReason = "transfer_limit"
                    } else {
                        chunks += PhotoChunk(rawOffset, imageBytes)
                        storedChunkBytes += imageBytes.size
                    }
                }
                null
            }

            EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END -> {
                if (!receiving) return null
                try {
                    assembleImage()
                } finally {
                    resetTransfer()
                }
            }

            else -> null
        }
    }

    @Synchronized
    fun reset() {
        resetTransfer()
    }

    private fun assembleImage(): ByteArray? {
        invalidReason?.let { return reject(it) }
        if (chunks.isEmpty()) return reject("no_data")

        // The glasses retain an unsigned 32-bit byte counter between photos.
        // Find the beginning of this bounded interval on the counter's circle,
        // independent of arrival order and even when the counter rolls over.
        val rawOrdered = chunks.sortedBy { it.rawOffset }
        val originIndex = rawOrdered.indices.maxBy { index ->
            val previous = rawOrdered[(index + rawOrdered.size - 1) % rawOrdered.size]
            (rawOrdered[index].rawOffset - previous.rawOffset) and UINT32_MASK
        }
        val origin = rawOrdered[originIndex].rawOffset
        if (origin != 0L && announcedBytes == null) {
            return reject("missing_size_for_rebase base=" + origin)
        }
        val ordered = mutableListOf<RelativeChunk>()
        for (chunk in chunks) {
            val relativeOffset = (chunk.rawOffset - origin) and UINT32_MASK
            if (relativeOffset + chunk.bytes.size > MAX_TRANSFER_BYTES) {
                return reject("offset_span_limit base=" + origin)
            }
            ordered += RelativeChunk(relativeOffset.toInt(), chunk.bytes)
        }
        ordered.sortWith(compareBy<RelativeChunk> { it.offset }.thenBy { it.bytes.size })

        var contiguousEnd = 0
        for (chunk in ordered) {
            if (chunk.offset > contiguousEnd) {
                return reject(
                    "gap offset=" + contiguousEnd + " next=" + chunk.offset +
                        " missing=" + (chunk.offset - contiguousEnd),
                )
            }
            contiguousEnd = maxOf(contiguousEnd, chunk.offset + chunk.bytes.size)
        }
        if (contiguousEnd <= 0 || contiguousEnd > MAX_TRANSFER_BYTES) {
            return reject("invalid_span bytes=" + contiguousEnd)
        }
        // Exact coverage prevents a missing initial chunk from being rebased to
        // an incidental JPEG marker later in the payload.
        if (announcedBytes?.let { contiguousEnd != it } == true) {
            return reject("size_mismatch actual=" + contiguousEnd)
        }

        val image = ByteArray(contiguousEnd)
        var writtenEnd = 0
        for (chunk in ordered) {
            val overlap = (writtenEnd - chunk.offset).coerceIn(0, chunk.bytes.size)
            for (index in 0 until overlap) {
                if (image[chunk.offset + index] != chunk.bytes[index]) {
                    return reject("overlap_conflict offset=" + (chunk.offset + index))
                }
            }
            if (overlap < chunk.bytes.size) {
                chunk.bytes.copyInto(
                    destination = image,
                    destinationOffset = chunk.offset + overlap,
                    startIndex = overlap,
                )
            }
            writtenEnd = maxOf(writtenEnd, chunk.offset + chunk.bytes.size)
        }
        if (image.size < 4 || image[0] != 0xFF.toByte() || image[1] != 0xD8.toByte()) {
            return reject("missing_jpeg_start base=" + origin)
        }

        var jpegEnd = image.size
        while (jpegEnd > 0 && image[jpegEnd - 1] == 0.toByte()) jpegEnd--
        val padding = image.size - jpegEnd
        if (padding > 3 || (padding > 0 && (announcedBytes == null || image.size % 4 != 0))) {
            return reject("invalid_padding bytes=" + padding)
        }
        if (
            jpegEnd < 4 ||
            image[jpegEnd - 2] != 0xFF.toByte() ||
            image[jpegEnd - 1] != 0xD9.toByte()
        ) {
            return reject("missing_jpeg_end padding=" + padding)
        }
        // Only remove confirmed terminal zero alignment, never interior bytes.
        return if (padding == 0) image else image.copyOf(jpegEnd)
    }

    private fun reject(reason: String): ByteArray? {
        onTransferRejected(
            reason + " chunks=" + chunks.size + " stored=" + storedChunkBytes +
                " announced=" + (announcedBytes?.toString() ?: "unknown"),
        )
        return null
    }

    private fun readUnsigned32(packet: ByteArray, start: Int): Long =
        ((packet[start].toLong() and 0xFF) shl 24) or
            ((packet[start + 1].toLong() and 0xFF) shl 16) or
            ((packet[start + 2].toLong() and 0xFF) shl 8) or
            (packet[start + 3].toLong() and 0xFF)

    private fun resetTransfer() {
        receiving = false
        invalidReason = null
        announcedBytes = null
        storedChunkBytes = 0
        chunks.clear()
    }
}
