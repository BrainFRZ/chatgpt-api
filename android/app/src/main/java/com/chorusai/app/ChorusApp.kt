package com.chorusai.app

import android.app.Application
import androidx.lifecycle.ProcessLifecycleOwner
import com.chorusai.app.lifecycle.AppLifecycleObserver
import dagger.hilt.android.HiltAndroidApp
import javax.inject.Inject

@HiltAndroidApp
class ChorusApp : Application() {

    @Inject lateinit var appLifecycleObserver: AppLifecycleObserver

    override fun onCreate() {
        super.onCreate()
        ProcessLifecycleOwner.get().lifecycle.addObserver(appLifecycleObserver)
    }
}
