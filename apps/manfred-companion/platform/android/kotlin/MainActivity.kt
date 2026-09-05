package com.thetopham.manfred_companion

import com.thetopham.manfred_companion.eyevue.EyevuePlugin
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    private var eyevue: EyevuePlugin? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        eyevue?.dispose()
        eyevue = EyevuePlugin(this, flutterEngine.dartExecutor.binaryMessenger)
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        eyevue?.dispose()
        eyevue = null
        super.cleanUpFlutterEngine(flutterEngine)
    }
}
