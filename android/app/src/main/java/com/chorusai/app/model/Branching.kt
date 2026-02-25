package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class SiblingInfo(
    val id: String,
    val index: Int,
    val total: Int
)

data class BranchInfoResponse(
    val siblings: List<SiblingInfo>,
    @SerializedName("current_index") val currentIndex: Int,
    @SerializedName("total_siblings") val totalSiblings: Int
)

data class SetBookmarkRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    @SerializedName("message_id") val messageId: String,
    val bookmark: String,
    val project: String? = null
)
