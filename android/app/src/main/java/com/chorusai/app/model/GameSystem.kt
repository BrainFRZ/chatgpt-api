package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class GameSystemInfo(
    val id: String,
    val name: String
)

data class SetProjectGameSystemRequest(
    val username: String,
    val project: String,
    @SerializedName("game_system") val gameSystem: String
)
