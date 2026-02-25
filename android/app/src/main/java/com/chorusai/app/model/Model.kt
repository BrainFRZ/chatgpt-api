package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class ModelPricing(
    @SerializedName("input_new") val inputNew: Double,
    @SerializedName("input_cached") val inputCached: Double,
    val output: Double,
    val reasoning: Double
)

data class ContextLimits(
    val threshold: Int,
    val target: Int
)

data class ModelInfo(
    val id: String,
    val name: String,
    val pricing: ModelPricing,
    @SerializedName("context_limits") val contextLimits: ContextLimits
)

data class SetChatModelRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    val project: String? = null,
    val model: String
)

data class SetProjectModelRequest(
    val username: String,
    val project: String,
    val model: String
)
