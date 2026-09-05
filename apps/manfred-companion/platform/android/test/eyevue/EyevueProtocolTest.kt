// Ported regression coverage; source hashes in kotlin/eyevue/SOURCE_PROVENANCE.md.
package com.thetopham.manfred_companion.eyevue

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevueProtocolTest {

    @Test
    fun deviceInfoReadUsesVendorGetCommandAndParsesIncomingFixture() {
        assertArrayEquals(
            byteArrayOf(0xAB.toByte(), 0x55, 0, 3, 0x55, 0, 0x55),
            EyevueProtocol.buildGetDeviceInfoPacket(),
        )
        val response = byteArrayOf(
            0xAC.toByte(), 0x55, 0, 9, 0x55, 1, 2, 3, 4, 5, 6, 7, 0x71,
        )
        assertEquals(
            EyevueDeviceInfo("1.2.3", "4.5.6", "7"),
            EyevueProtocol.parseDeviceInfo(EyevueProtocol.parseDatagram(response)),
        )
    }

    @Test
    fun deviceInfoVersionsAreUnsignedAndIgnoreExtensionBytes() {
        val response = EyevueFrame(
            EyevueProtocol.CMD_GET_DEVICE_INFO,
            byteArrayOf(0xff.toByte(), 0x80.toByte(), 0, 1, 2, 0xfe.toByte(), 0xff.toByte(), 9),
        )
        assertEquals(
            EyevueDeviceInfo("255.128.0", "1.2.254", "255"),
            EyevueProtocol.parseDeviceInfo(response),
        )
    }

    @Test
    fun deviceInfoRequiresCorrectReplyAndSevenPayloadBytes() {
        assertEquals(null, EyevueProtocol.parseDeviceInfo(EyevueFrame(0x65, ByteArray(7))))
        for (length in 0 until 7) {
            assertEquals(
                null,
                EyevueProtocol.parseDeviceInfo(EyevueFrame(EyevueProtocol.CMD_GET_DEVICE_INFO, ByteArray(length))),
            )
        }
    }
    @Test
    fun livePacketsMatchVendorFrames() {
        assertArrayEquals(
            byteArrayOf(0xAB.toByte(), 0x55, 0x00, 0x03, 0x67, 0x30, 0x97.toByte()),
            EyevueProtocol.buildStartLiveApPacket(),
        )
        assertArrayEquals(
            byteArrayOf(0xAB.toByte(), 0x55, 0x00, 0x04, 0x44, 0x30, 0x01, 0x75),
            EyevueProtocol.buildFinishTransferPacket(),
        )
    }

    @Test
    fun decoderHandlesFragmentedFrames() {
        val decoder = EyevueFrameDecoder()
        val packet = EyevueProtocol.buildStartLiveP2pPacket()

        assertTrue(decoder.append(packet.copyOfRange(0, 3)).isEmpty())
        val frames = decoder.append(packet.copyOfRange(3, packet.size))

        assertEquals(1, frames.size)
        assertEquals(EyevueProtocol.CMD_APP_LIVE, frames.single().commandId)
        assertArrayEquals(byteArrayOf(0x31), frames.single().payload)
    }

    @Test
    fun parserRejectsCorruptCrc() {
        val packet = EyevueProtocol.buildStartLiveApPacket().also { it[it.lastIndex] = 0 }

        runCatching { EyevueProtocol.parseDatagram(packet) }
            .onSuccess { error("Corrupt packet was accepted") }
    }

    @Test
    fun parsesBatteryAndWifiResponses() {
        val battery = EyevueProtocol.parseBattery(
            EyevueFrame(EyevueProtocol.CMD_GET_BATTERY, byteArrayOf(0x07, 0x05, 0x01)),
        )
        assertEquals(75, battery?.percent)
        assertTrue(battery?.isCharging == true)

        val wifi = EyevueProtocol.parseWifiSsid(
            EyevueFrame(EyevueProtocol.CMD_RECEIVE_WIFI_INFO, "Eyevue-AP\u0000".toByteArray()),
        )
        assertEquals("Eyevue-AP", wifi)
    }

    @Test
    fun voiceAssistantPacketsMatchVendorBitFlags() {
        assertArrayEquals(
            EyevueProtocol.valuePacket(EyevueProtocol.CMD_SET_VOICE_ASSISTANT_STATUS, 1),
            EyevueProtocol.buildSetVoiceAssistantStatusPacket(
                localOfflineSpeechEnabled = false,
                aiWakeWordEnabled = true,
            ),
        )
        val status = EyevueProtocol.parseVoiceAssistantStatus(
            EyevueFrame(EyevueProtocol.CMD_GET_VOICE_ASSISTANT_STATUS, byteArrayOf(1)),
        )
        assertEquals(false, status?.localOfflineSpeechEnabled)
        assertEquals(true, status?.aiWakeWordEnabled)
    }

    @Test
    fun parsesIncomingAcBatteryCustomerAndStatusFixtures() {
        val battery = EyevueProtocol.parseBattery(EyevueProtocol.parseDatagram(incomingBatteryPacket()))
        assertEquals(EyevueBattery(percent = 86, isCharging = false), battery)

        val customer = EyevueProtocol.parseCustomer(EyevueProtocol.parseDatagram(incomingCustomerPacket()))
        assertEquals(EyevueCustomer(project = "TK8", customer = "G6"), customer)

        val status = EyevueProtocol.parseVoiceAssistantStatus(
            EyevueProtocol.parseDatagram(incomingStatusPacket()),
        )
        assertEquals(
            EyevueVoiceAssistantStatus(localOfflineSpeechEnabled = false, aiWakeWordEnabled = true),
            status,
        )
    }

    @Test
    fun decoderHandlesEveryIncomingFrameSplitIncludingHeader() {
        for (packet in listOf(incomingBatteryPacket(), incomingCustomerPacket(), incomingStatusPacket())) {
            for (split in 1 until packet.size) {
                val decoder = EyevueFrameDecoder()
                assertTrue(decoder.append(packet.copyOfRange(0, split)).isEmpty())
                val actual = decoder.append(packet.copyOfRange(split, packet.size)).single()
                val expected = EyevueProtocol.parseDatagram(packet)
                assertEquals(expected.commandId, actual.commandId)
                assertArrayEquals(expected.payload, actual.payload)
            }
        }
    }

    @Test
    fun decoderHandlesIncomingAndLegacyFramesMixedWithNoise() {
        val decoder = EyevueFrameDecoder()
        val stream = byteArrayOf(0, 0x55, 0xAB.toByte(), 0x12, 0xAC.toByte()) +
            incomingBatteryPacket() + byteArrayOf(0xAD.toByte(), 0x55, 0, 0) +
            EyevueProtocol.buildStartLiveP2pPacket() + incomingStatusPacket()
        val frames = mutableListOf<EyevueFrame>()
        // Single-byte notifications also exercise retaining a trailing partial header.
        for (byte in stream) frames += decoder.append(byteArrayOf(byte))
        assertEquals(listOf(0x17, 0x67, 0x71), frames.map { it.commandId })
        assertArrayEquals(byteArrayOf(0x38, 0x36, 0), frames[0].payload)
        assertArrayEquals(byteArrayOf(0x31), frames[1].payload)
        assertArrayEquals(byteArrayOf(1), frames[2].payload)
    }

    @Test
    fun parserRejectsIncomingCrcLengthAndHeaderErrors() {
        val valid = incomingBatteryPacket()
        val invalidPackets = listOf(
            valid.copyOf().also { it[it.lastIndex] = 0 },
            valid.copyOf().also { it[3] = 4 },
            valid.copyOf().also { it[3] = 1 },
            valid.copyOf(valid.size - 1),
            valid + byteArrayOf(0),
            valid.copyOf().also { it[0] = 0xAD.toByte() },
            valid.copyOf().also { it[1] = 0x54 },
        )
        for (packet in invalidPackets) {
            assertTrue(runCatching { EyevueProtocol.parseDatagram(packet) }.isFailure)
        }
    }

    @Test
    fun decoderRejectsIncomingCorruptFrameAndContinuesAtNextFrame() {
        val decoder = EyevueFrameDecoder()
        val corrupt = incomingBatteryPacket().also { it[it.lastIndex] = 0 }
        val frames = decoder.append(corrupt + incomingCustomerPacket())
        assertEquals(1, frames.size)
        assertEquals(EyevueCustomer("TK8", "G6"), EyevueProtocol.parseCustomer(frames.single()))
    }

    @Test
    fun decoderRejectsInvalidIncomingLengthAndResynchronizes() {
        val decoder = EyevueFrameDecoder()
        val invalidLength = byteArrayOf(0xAC.toByte(), 0x55, 0, 1, 0)
        val frames = decoder.append(invalidLength + incomingStatusPacket())
        assertEquals(1, frames.size)
        assertEquals(0x71, frames.single().commandId)
        assertArrayEquals(byteArrayOf(1), frames.single().payload)
    }

    @Test
    fun decoderResetDropsPartialIncomingFrame() {
        val decoder = EyevueFrameDecoder()
        val packet = incomingBatteryPacket()
        assertTrue(decoder.append(packet.copyOfRange(0, 4)).isEmpty())
        decoder.reset()
        val frames = decoder.append(incomingStatusPacket())
        assertEquals(listOf(0x71), frames.map { it.commandId })
    }

    @Test
    fun pullImagePacketPreservesOpaqueByteAndOutgoingHeader() {
        for ((type, checksum) in listOf(0 to 0x36, 1 to 0x37, 255 to 0x35)) {
            assertArrayEquals(
                byteArrayOf(0xAB.toByte(), 0x55, 0, 3, 0x36, type.toByte(), checksum.toByte()),
                EyevueProtocol.buildPullImagePacket(type),
            )
        }
    }

    @Test
    fun pullImagePacketRejectsValuesThatWouldOtherwiseWrap() {
        for (type in listOf(-1, 256, Int.MIN_VALUE, Int.MAX_VALUE)) {
            assertTrue(runCatching { EyevueProtocol.buildPullImagePacket(type) }.isFailure)
        }
    }

    // Explicit wire fixtures keep incoming magic, lengths and checksums independent
    // from buildDatagram(), which must continue to produce outgoing AB55 frames.
    private fun incomingBatteryPacket(): ByteArray =
        byteArrayOf(0xAC.toByte(), 0x55, 0, 5, 0x17, 0x38, 0x36, 0, 0x85.toByte())

    private fun incomingCustomerPacket(): ByteArray =
        byteArrayOf(0xAC.toByte(), 0x55, 0, 10, 0x64, 0x54, 0x4B, 0x38, 0, 0x47, 0x36, 0, 0, 0xB8.toByte())

    private fun incomingStatusPacket(): ByteArray =
        byteArrayOf(0xAC.toByte(), 0x55, 0, 3, 0x71, 1, 0x72)

    @Test
    fun photoAssemblerUsesDeclaredOffsetsInsteadOfArrivalOrder() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(photoDataPacket(offset = 2, bytes = byteArrayOf(0xFF.toByte(), 0xD9.toByte())))
        assembler.append(photoDataPacket(offset = 0, bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte())))

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertArrayEquals(
            byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte()),
            image,
        )
    }

    @Test
    fun photoAssemblerIgnoresExactDuplicateChunk() {
        val assembler = EyevuePhotoAssembler()
        val firstChunk = photoDataPacket(offset = 0, bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte()))
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(firstChunk)
        assembler.append(firstChunk)
        assembler.append(photoDataPacket(offset = 2, bytes = byteArrayOf(0xFF.toByte(), 0xD9.toByte())))

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertArrayEquals(
            byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte()),
            image,
        )
    }

    @Test
    fun photoAssemblerRejectsGapInsteadOfReturningCorruptImage() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(photoDataPacket(offset = 0, bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte())))
        assembler.append(photoDataPacket(offset = 3, bytes = byteArrayOf(0xD9.toByte())))

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertEquals(null, image)
    }

    @Test
    fun photoAssemblerAcceptsCompatibleOverlapAcrossOffsets() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(
            photoDataPacket(
                offset = 0,
                bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x01),
            ),
        )
        assembler.append(
            photoDataPacket(
                offset = 2,
                bytes = byteArrayOf(0x01, 0xFF.toByte(), 0xD9.toByte()),
            ),
        )

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertArrayEquals(
            byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x01, 0xFF.toByte(), 0xD9.toByte()),
            image,
        )
    }

    @Test
    fun photoAssemblerAcceptsCompatibleSameStartExtension() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(
            photoDataPacket(
                offset = 0,
                bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte()),
            ),
        )
        assembler.append(
            photoDataPacket(
                offset = 0,
                bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x01, 0xFF.toByte(), 0xD9.toByte()),
            ),
        )

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertArrayEquals(
            byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x01, 0xFF.toByte(), 0xD9.toByte()),
            image,
        )
    }

    @Test
    fun photoAssemblerRejectsConflictingOverlapAcrossOffsets() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(photoDataPacket(offset = 0, bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0x03)))
        assembler.append(photoDataPacket(offset = 2, bytes = byteArrayOf(0x09, 0xFF.toByte(), 0xD9.toByte())))

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertEquals(null, image)
    }

    @Test
    fun photoAssemblerRejectsConflictingDuplicateOffset() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(photoDataPacket(offset = 0, bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte())))
        assembler.append(photoDataPacket(offset = 0, bytes = byteArrayOf(0x00, 0x00)))

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertEquals(null, image)
    }

    @Test
    fun photoAssemblerRejectsContiguousPrefixWithoutJpegEndMarker() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoStartPacket(announcedBytes = 2))
        assembler.append(
            photoDataPacket(
                offset = 0,
                bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte()),
            ),
        )

        val image = assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END))

        assertEquals(null, image)
    }

    @Test
    fun photoAssemblerResetDropsInProgressTransfer() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(photoDataPacket(offset = 0, bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte())))

        assembler.reset()

        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))

        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(
            photoDataPacket(
                offset = 0,
                bytes = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte()),
            ),
        )
        assertArrayEquals(
            byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte()),
            assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
        )
    }

    @Test
    fun photoAssemblerAcceptsConsecutiveCumulativeCountersFromS25Trace() {
        val assembler = EyevuePhotoAssembler()
        val bases = listOf(0L, 10_344L, 20_356L, 30_292L)
        val sizes = listOf(10_344, 10_012, 9_936, 10_284)
        for ((base, size) in bases.zip(sizes)) {
            val wireImage = alignedWireImage(size, padding = 3)
            assembler.append(photoStartPacket(size))
            appendWireImage(assembler, base, wireImage)
            assertArrayEquals(
                wireImage.copyOf(size - 3),
                assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
            )
        }
    }

    @Test
    fun photoAssemblerRebasesOutOfOrderChunksWithoutUsingArrivalOrder() {
        val assembler = EyevuePhotoAssembler()
        val wireImage = alignedWireImage(8, padding = 0)
        assembler.append(photoStartPacket(wireImage.size))
        assembler.append(photoDataPacket(10_348, wireImage.copyOfRange(4, 8)))
        assembler.append(photoDataPacket(10_344, wireImage.copyOfRange(0, 4)))
        assertArrayEquals(
            wireImage,
            assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
        )
    }

    @Test
    fun photoAssemblerHandlesUnsignedCounterRolloverOutOfOrder() {
        val assembler = EyevuePhotoAssembler()
        val wireImage = alignedWireImage(8, padding = 0)
        assembler.append(photoStartPacket(wireImage.size))
        assembler.append(photoDataPacket(2, wireImage.copyOfRange(4, 8)))
        assembler.append(photoDataPacket(0xFFFF_FFFEL, wireImage.copyOfRange(0, 4)))
        assertArrayEquals(
            wireImage,
            assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
        )
    }

    @Test
    fun photoAssemblerAppliesMemoryLimitToPhotoNotCumulativeCounter() {
        val assembler = EyevuePhotoAssembler()
        val wireImage = alignedWireImage(8, padding = 0)
        assembler.append(photoStartPacket(wireImage.size))
        assembler.append(photoDataPacket(0xF000_0000L, wireImage))
        assertArrayEquals(
            wireImage,
            assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
        )
    }

    @Test
    fun photoAssemblerRejectsMissingInitialChunkWithIncidentalJpegMarker() {
        val rejected = mutableListOf<String>()
        val assembler = EyevuePhotoAssembler(rejected::add)
        assembler.append(photoStartPacket(12))
        // A later payload could coincidentally start with SOI. It is still not
        // the entire announced transfer and must not become a replacement origin.
        assembler.append(photoDataPacket(10_348, alignedWireImage(8, padding = 0)))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        assertTrue(rejected.single().startsWith("size_mismatch"))
    }

    @Test
    fun photoAssemblerRejectsRebaseWithoutAnnouncedSize() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(photoDataPacket(4, alignedWireImage(8, padding = 0)))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
    }

    @Test
    fun photoAssemblerRejectsMissingJpegStartEvenWhenSizeMatches() {
        val assembler = EyevuePhotoAssembler()
        val wireImage = alignedWireImage(8, padding = 0).also { it[0] = 0x7F }
        assembler.append(photoStartPacket(wireImage.size))
        assembler.append(photoDataPacket(10_344, wireImage))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
    }

    @Test
    fun photoAssemblerPreservesGapRejectionAfterRebaseWithUsefulDiagnostics() {
        val rejected = mutableListOf<String>()
        val assembler = EyevuePhotoAssembler(rejected::add)
        val wireImage = alignedWireImage(10_344, padding = 3)
        assembler.append(photoStartPacket(wireImage.size))
        appendWireImage(assembler, 10_344, wireImage, skipOffset = 4_960)
        assertTrue(rejected.isEmpty())
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        assertTrue(rejected.single().contains("gap offset=4960 next=5456 missing=496"))
        assertTrue(rejected.single().contains("announced=10344"))
        assertTrue(rejected.single().contains("chunks="))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        assertEquals(1, rejected.size)
    }

    @Test
    fun photoAssemblerAcceptsOnlyBoundedZeroWordPadding() {
        for (padding in 0..3) {
            val assembler = EyevuePhotoAssembler()
            val wireImage = alignedWireImage(8, padding)
            assembler.append(photoStartPacket(wireImage.size))
            assembler.append(photoDataPacket(10_344, wireImage))
            assertArrayEquals(
                wireImage.copyOf(wireImage.size - padding),
                assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
            )
        }
    }

    @Test
    fun photoAssemblerUsesS25TerminalPacketLayoutAndPreservesInteriorBytes() {
        val assembler = EyevuePhotoAssembler()
        val wireImage = alignedWireImage(9_936, padding = 3)
        val syntheticTail = byteArrayOf(
            0x11, 0x22, 0x33, 0xFF.toByte(), 0,
            0x44, 0x55, 0x66, 0x77, 0x12, 0x34,
            0xFF.toByte(), 0xD9.toByte(), 0, 0, 0,
        )
        syntheticTail.copyInto(wireImage, wireImage.size - syntheticTail.size)
        assembler.append(photoStartPacket(wireImage.size))
        appendWireImage(assembler, 20_356, wireImage)
        assertArrayEquals(
            wireImage.copyOf(9_933),
            assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
        )
    }

    @Test
    fun photoAssemblerRejectsExcessOrNonzeroTrailingBytes() {
        val invalidImages = listOf(
            byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte(), 0, 0, 0, 0),
            byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte(), 1, 0, 0, 0),
        )
        for (wireImage in invalidImages) {
            val assembler = EyevuePhotoAssembler()
            assembler.append(photoStartPacket(wireImage.size))
            assembler.append(photoDataPacket(10_344, wireImage))
            assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        }
    }

    @Test
    fun photoAssemblerRejectsPaddingWithoutAnnouncedWordAlignment() {
        val image = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte(), 0)
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoStartPacket(image.size))
        assembler.append(photoDataPacket(0, image))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START))
        assembler.append(photoDataPacket(0, alignedWireImage(8, padding = 3)))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
    }

    @Test
    fun photoAssemblerUsesAllFourAnnouncedSizeBytes() {
        val assembler = EyevuePhotoAssembler()
        assembler.append(photoStartPacket(65_540))
        assembler.append(photoDataPacket(0, byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xD9.toByte())))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))

        val wireImage = alignedWireImage(65_540, padding = 0)
        assembler.append(photoStartPacket(wireImage.size))
        appendWireImage(assembler, 0, wireImage)
        assertArrayEquals(
            wireImage,
            assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
        )
    }

    @Test
    fun photoAssemblerRejectsDeclaredOrRelativeSpanBeyondLimit() {
        val rejected = mutableListOf<String>()
        val assembler = EyevuePhotoAssembler(rejected::add)
        assembler.append(photoStartPacket(32 * 1024 * 1024 + 1))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        assertTrue(rejected.last().startsWith("invalid_announced_size"))

        assembler.append(photoStartPacket(8))
        assembler.append(photoDataPacket(0, byteArrayOf(0xFF.toByte(), 0xD8.toByte())))
        assembler.append(photoDataPacket(32L * 1024 * 1024, byteArrayOf(0xFF.toByte(), 0xD9.toByte())))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        assertTrue(rejected.last().startsWith("offset_span_limit"))
    }

    @Test
    fun photoAssemblerKeepsConflictChecksAfterRolloverAndResetsAfterFailure() {
        val rejected = mutableListOf<String>()
        val assembler = EyevuePhotoAssembler(rejected::add)
        val wireImage = alignedWireImage(8, padding = 0)
        assembler.append(photoStartPacket(wireImage.size))
        assembler.append(photoDataPacket(0xFFFF_FFFCL, wireImage))
        assembler.append(photoDataPacket(0, byteArrayOf(0x01)))
        assertEquals(null, assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)))
        assertTrue(rejected.single().startsWith("overlap_conflict offset=4"))

        assembler.append(photoStartPacket(wireImage.size))
        assembler.append(photoDataPacket(20_356, wireImage))
        assertArrayEquals(
            wireImage,
            assembler.append(photoPacket(EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_END)),
        )
        assertEquals(1, rejected.size)
    }

    private fun alignedWireImage(size: Int, padding: Int): ByteArray {
        require(size - padding >= 4)
        return ByteArray(size) { 0x5A.toByte() }.also { image ->
            image[0] = 0xFF.toByte()
            image[1] = 0xD8.toByte()
            image[size - padding - 2] = 0xFF.toByte()
            image[size - padding - 1] = 0xD9.toByte()
            for (index in size - padding until size) image[index] = 0
        }
    }

    private fun appendWireImage(
        assembler: EyevuePhotoAssembler,
        base: Long,
        image: ByteArray,
        skipOffset: Int? = null,
    ) {
        for (offset in image.indices step 496) {
            if (offset == skipOffset) continue
            assembler.append(
                photoDataPacket(
                    (base + offset) and 0xFFFF_FFFFL,
                    image.copyOfRange(offset, minOf(offset + 496, image.size)),
                ),
            )
        }
    }

    private fun photoStartPacket(announcedBytes: Int): ByteArray =
        photoPacket(
            EyevueProtocol.CMD_RECEIVE_PHOTO_DATA_START,
            byteArrayOf(
                ((announcedBytes ushr 24) and 0xFF).toByte(),
                ((announcedBytes ushr 16) and 0xFF).toByte(),
                ((announcedBytes ushr 8) and 0xFF).toByte(),
                (announcedBytes and 0xFF).toByte(),
                2,
            ),
        )

    private fun photoDataPacket(offset: Long, bytes: ByteArray): ByteArray =
        photoPacket(
            EyevueProtocol.CMD_RECEIVE_PHOTO_DATA,
            byteArrayOf(
                ((offset ushr 24) and 0xFF).toByte(),
                ((offset ushr 16) and 0xFF).toByte(),
                ((offset ushr 8) and 0xFF).toByte(),
                (offset and 0xFF).toByte(),
            ) + bytes,
        )

    private fun photoPacket(commandId: Int, payload: ByteArray = byteArrayOf()): ByteArray =
        byteArrayOf(0x52, 0x58, 0, 0, commandId.toByte()) + payload + byteArrayOf(0, 0x58, 0x52)
}
