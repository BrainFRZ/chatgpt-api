package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class SetAnthropicSyncRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    val project: String? = null,
    val sync: Boolean
)

data class ReloadChatRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    val project: String? = null
)

data class SaveUpdatesRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    val updates: String,
    val project: String? = null
)

data class CountTokensRequest(
    val text: String
)
