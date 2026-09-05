package com.thetopham.manfred_companion

import android.content.Context
import java.io.File

object ChatMirrorQuota {
    const val MAX_PENDING_RECORDS = 10_000
    const val OVERFLOW_RESERVED_RECORDS = 64
    const val MAX_PENDING_BYTES = 256L * 1024L * 1024L
    const val OVERFLOW_RESERVE_BYTES = 1024L * 1024L

    private const val PREFERENCES = "chat_mirror_quota"
    private const val KEY_INITIALIZED = "initialized"
    private const val KEY_RECORDS = "records"
    private const val KEY_BYTES = "bytes"

    data class Stats(val records: Int, val bytes: Long)

    @Synchronized
    fun <T> locked(block: () -> T): T = block()

    @Synchronized
    fun reconcile(context: Context, directory: File): Stats {
        val files = directory.listFiles().orEmpty().filter(File::isFile)
        val recordNames = files.mapNotNull { file ->
            when {
                file.name.endsWith(".json.bak") -> file.name.removeSuffix(".bak")
                file.name.endsWith(".json") -> file.name
                else -> null
            }
        }.toSet()
        return save(context, Stats(recordNames.size, files.sumOf(File::length)))
    }

    @Synchronized
    fun current(context: Context, directory: File): Stats {
        val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
        if (!preferences.getBoolean(KEY_INITIALIZED, false)) {
            return reconcile(context, directory)
        }
        return Stats(
            records = preferences.getInt(KEY_RECORDS, 0).coerceAtLeast(0),
            bytes = preferences.getLong(KEY_BYTES, 0L).coerceAtLeast(0L),
        )
    }

    @Synchronized
    fun recordReplacement(
        context: Context,
        directory: File,
        existed: Boolean,
        oldBytes: Long,
        newBytes: Long,
    ): Stats {
        val current = current(context, directory)
        return save(
            context,
            Stats(
                records = current.records + if (existed) 0 else 1,
                bytes = (current.bytes - oldBytes + newBytes).coerceAtLeast(0L),
            ),
        )
    }

    @Synchronized
    fun recordDeletion(
        context: Context,
        directory: File,
        existed: Boolean,
        deletedBytes: Long,
    ): Stats {
        val current = current(context, directory)
        return save(
            context,
            Stats(
                records = (current.records - if (existed) 1 else 0).coerceAtLeast(0),
                bytes = (current.bytes - deletedBytes).coerceAtLeast(0L),
            ),
        )
    }

    private fun save(context: Context, stats: Stats): Stats {
        val committed = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_INITIALIZED, true)
            .putInt(KEY_RECORDS, stats.records)
            .putLong(KEY_BYTES, stats.bytes)
            .commit()
        check(committed) { "Could not persist Chat Mirror quota accounting" }
        return stats
    }
}
