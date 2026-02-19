package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class PacingInfo(
    val episode: Int = 1,
    val beat: Int = 1,
    val responses: Int = 0
)

data class CallbackEntry(
    val id: Int,
    @SerializedName("created_turn") val createdTurn: Int,
    val description: String? = null,
    val type: String? = null,
    val trigger: String? = null,
    val status: String? = null
)

data class CallbackLedger(
    @SerializedName("next_id") val nextId: Int = 1,
    val open: List<CallbackEntry> = emptyList(),
    @SerializedName("recently_resolved") val recentlyResolved: List<CallbackEntry> = emptyList()
)

data class NpcMemory(
    val high: List<String> = emptyList(),
    val moderate: List<String> = emptyList(),
    val flavor: List<String> = emptyList()
)

data class CharacterState(
    val data: Map<String, Any> = emptyMap(),
    @SerializedName("last_updated") val lastUpdated: Int = 0
)

data class PipelineState(
    val pacing: PacingInfo = PacingInfo(),
    @SerializedName("callback_ledger") val callbackLedger: CallbackLedger = CallbackLedger(),
    @SerializedName("npc_memories") val npcMemories: Map<String, NpcMemory> = emptyMap(),
    @SerializedName("scene_state") val sceneState: Map<String, Any> = emptyMap(),
    @SerializedName("character_states") val characterStates: Map<String, CharacterState> = emptyMap(),
    @SerializedName("game_state") val gameState: Map<String, Any> = emptyMap(),
    @SerializedName("hud_state") val hudState: Map<String, Any>? = null,
    val combat: Map<String, Any>? = null,
    @SerializedName("turn_counter") val turnCounter: Int = 0
)
