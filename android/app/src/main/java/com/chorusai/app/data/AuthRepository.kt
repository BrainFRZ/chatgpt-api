package com.chorusai.app.data

import com.chorusai.app.model.*
import com.chorusai.app.network.ChorusApi
import retrofit2.Response
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class AuthRepository @Inject constructor(
    private val api: ChorusApi,
    private val prefs: UserPreferences
) {
    suspend fun login(username: String): Response<LoginResponse> =
        api.login(LoginRequest(username))

    suspend fun setApiKeys(
        username: String,
        openaiKey: String? = null,
        anthropicKey: String? = null
    ): Response<ApiKeysResponse> =
        api.setApiKeys(ApiKeysRequest(username, openaiKey, anthropicKey))

    suspend fun getApiKeys(username: String): Response<ApiKeysResponse> =
        api.getApiKeys(username)

    suspend fun saveLogin(username: String) = prefs.saveLogin(username)

    suspend fun logout() = prefs.logout()
}
