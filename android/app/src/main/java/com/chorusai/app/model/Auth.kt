package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class LoginRequest(
    val username: String
)

data class LoginResponse(
    val username: String,
    @SerializedName("has_api_key") val hasApiKey: Boolean,
    @SerializedName("is_new_user") val isNewUser: Boolean
)

data class ApiKeyRequest(
    val username: String,
    @SerializedName("api_key") val apiKey: String
)

data class ApiKeysRequest(
    val username: String,
    @SerializedName("openai_key") val openaiKey: String? = null,
    @SerializedName("anthropic_key") val anthropicKey: String? = null
)

data class ApiKeysResponse(
    @SerializedName("has_openai") val hasOpenai: Boolean,
    @SerializedName("has_anthropic") val hasAnthropic: Boolean
)
