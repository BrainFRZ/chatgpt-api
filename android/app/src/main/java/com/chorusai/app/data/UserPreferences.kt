package com.chorusai.app.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import javax.inject.Inject
import javax.inject.Singleton

private val Context.dataStore by preferencesDataStore(name = "user_prefs")

@Singleton
class UserPreferences @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val usernameKey = stringPreferencesKey("username")
    private val loggedInKey = booleanPreferencesKey("is_logged_in")
    private val lastChatNameKey = stringPreferencesKey("last_chat_name")
    private val lastChatProjectKey = stringPreferencesKey("last_chat_project")

    val username: Flow<String?> = context.dataStore.data.map { prefs ->
        prefs[usernameKey]
    }

    val isLoggedIn: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[loggedInKey] == true
    }

    suspend fun getLastChat(): Pair<String, String?>? {
        val prefs = context.dataStore.data.first()
        val chatName = prefs[lastChatNameKey] ?: return null
        val project = prefs[lastChatProjectKey]
        return chatName to project
    }

    suspend fun saveLastChat(chatName: String, project: String?) {
        context.dataStore.edit { prefs ->
            prefs[lastChatNameKey] = chatName
            if (project != null) {
                prefs[lastChatProjectKey] = project
            } else {
                prefs.remove(lastChatProjectKey)
            }
        }
    }

    suspend fun clearLastChat() {
        context.dataStore.edit { prefs ->
            prefs.remove(lastChatNameKey)
            prefs.remove(lastChatProjectKey)
        }
    }

    suspend fun saveLogin(username: String) {
        context.dataStore.edit { prefs ->
            prefs[usernameKey] = username
            prefs[loggedInKey] = true
        }
    }

    suspend fun logout() {
        context.dataStore.edit { prefs ->
            prefs.remove(usernameKey)
            prefs.remove(lastChatNameKey)
            prefs.remove(lastChatProjectKey)
            prefs[loggedInKey] = false
        }
    }
}
