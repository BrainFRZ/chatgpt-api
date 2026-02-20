package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

/** Converts Any? to display string, stripping ".0" from whole Doubles (Gson deserializes JSON ints as Double for Any? fields) */
internal fun Any?.toCleanString(): String = when (this) {
    is Double -> if (this == toLong().toDouble()) toLong().toString() else toString()
    else -> toString()
}

/** Display a Double cleanly: 14.0 → "14", 0.75 → "0.75" */
private fun Double.cleanNum(): String =
    if (this == toLong().toDouble()) toLong().toString() else toString()

data class VitalBar(
    val label: String = "",
    val current: Double? = null,
    val max: Double? = null,
    val value: Any? = null
) {
    val isFlat: Boolean get() = current == null && max == null && value != null
    val displayValue: String get() = when {
        isFlat -> value.toCleanString()
        current != null && max != null -> "${current.cleanNum()}/${max.cleanNum()}"
        current != null -> current.cleanNum()
        else -> ""
    }
}

data class ResourceEntry(
    val label: String = "",
    val current: Double = 0.0,
    val max: Double = 0.0
)

data class CharacterData(
    val type: String? = null,
    @SerializedName("class") val charClass: String? = null,
    val level: Any? = null,
    val vitals: List<VitalBar> = emptyList(),
    val resources: List<ResourceEntry> = emptyList(),
    val conditions: List<String> = emptyList(),
    val summary: String? = null
)

data class CharacterState(
    val data: CharacterData = CharacterData(),
    @SerializedName("last_updated") val lastUpdated: Int = 0
)

data class SceneState(
    @SerializedName("pcs_present") val pcsPresent: List<String> = emptyList(),
    @SerializedName("npcs_present") val npcsPresent: List<String> = emptyList()
)

data class CombatState(
    val round: Int = 0,
    @SerializedName("initiative_order") val initiativeOrder: List<String> = emptyList(),
    @SerializedName("current_turn") val currentTurn: String? = null
)

data class CallbackEntry(
    val id: Int = 0,
    @SerializedName("created_turn") val createdTurn: Int = 0,
    val description: String? = null,
    val type: String? = null,
    val trigger: String? = null,
    val status: String? = null,
    @SerializedName("original_text") val originalText: String? = null,
    @SerializedName("source_npc") val sourceNpc: String? = null,
    val resolutions: List<String>? = null,
    @SerializedName("resolved_turn") val resolvedTurn: Int? = null
)

data class CallbackLedger(
    @SerializedName("next_id") val nextId: Int = 1,
    val open: List<CallbackEntry> = emptyList(),
    @SerializedName("recently_resolved") val recentlyResolved: List<CallbackEntry> = emptyList()
)

data class NpcMemoryEntry(
    val text: String? = null,
    val memory: String? = null,
    val quote: String? = null,
    val date: String? = null,
    val impact: Int = 0,
    @SerializedName("turn_created") val turnCreated: Int = 0,
    val focus: String? = null
)

data class PacingInfo(
    val episode: Any? = 1,
    val beat: Any? = 1,
    val responses: Int = 0
)

data class HudState(
    val funds: Map<String, Any>? = null
)

data class PipelineState(
    val pacing: PacingInfo = PacingInfo(),
    @SerializedName("callback_ledger") val callbackLedger: CallbackLedger = CallbackLedger(),
    @SerializedName("npc_memories") val npcMemories: Map<String, List<NpcMemoryEntry>> = emptyMap(),
    @SerializedName("scene_state") val sceneState: SceneState = SceneState(),
    @SerializedName("character_states") val characterStates: Map<String, CharacterState> = emptyMap(),
    @SerializedName("game_state") val gameState: Map<String, Any> = emptyMap(),
    @SerializedName("hud_state") val hudState: HudState? = null,
    val combat: CombatState? = null,
    @SerializedName("turn_counter") val turnCounter: Int = 0
)
