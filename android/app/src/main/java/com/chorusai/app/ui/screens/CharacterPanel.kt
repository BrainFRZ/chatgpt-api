package com.chorusai.app.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.chorusai.app.model.*
import com.chorusai.app.ui.theme.*
import com.mikepenz.markdown.compose.components.markdownComponents
import com.mikepenz.markdown.compose.elements.highlightedCodeBlock
import com.mikepenz.markdown.compose.elements.highlightedCodeFence
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.m3.markdownColor
import com.mikepenz.markdown.m3.markdownTypography

// ─── Color Helpers ───

private fun vitalColor(current: Double?, max: Double?, label: String?): Color {
    val cur = current ?: return VitalGreen
    val mx = max ?: return VitalGreen
    if (mx <= 0.0) return VitalGreen
    val pct = (cur / mx).toFloat()
    val l = (label ?: "").lowercase()
    return when {
        l.contains("san") -> when {
            pct > 0.6f -> VitalSanHigh
            pct > 0.3f -> VitalSanMid
            else -> VitalSanLow
        }
        l.contains("human") -> when {
            pct > 0.6f -> VitalHumanityHigh
            pct > 0.3f -> VitalYellow
            else -> VitalRed
        }
        l.contains("hull") -> when {
            pct > 0.6f -> VitalHullHigh
            pct > 0.3f -> ConditionWarning
            else -> VitalRed
        }
        l.contains("shield") -> when {
            pct > 0.6f -> VitalShieldHigh
            pct > 0.3f -> ConditionWarning
            else -> VitalRed
        }
        else -> when {
            pct > 0.6f -> VitalGreen
            pct > 0.3f -> VitalYellow
            else -> VitalRed
        }
    }
}

private val dangerRegex = Regex("poison|bleed|burn|wound|dying|dead|critical", RegexOption.IGNORE_CASE)
private val warningRegex = Regex("exhaust|fright|stun|prone|restrain|shock|disabl|immobi", RegexOption.IGNORE_CASE)
private val positiveRegex = Regex("bless|haste|shield|invis|inspir|protect|rage|enhance", RegexOption.IGNORE_CASE)

private fun conditionColor(condition: String): Color = when {
    dangerRegex.containsMatchIn(condition) -> ConditionDanger
    warningRegex.containsMatchIn(condition) -> ConditionWarning
    positiveRegex.containsMatchIn(condition) -> ConditionPositive
    else -> ConditionNeutral
}

private fun typeBadgeColor(type: String?): Color = when (type?.lowercase()) {
    "pc" -> TypeBadgePC
    "enemy" -> TypeBadgeEnemy
    "ship" -> TypeBadgeShip
    else -> TypeBadgeNPC
}

private val spellRegex = Regex("spell", RegexOption.IGNORE_CASE)
private val techRegex = Regex("tech|cyber", RegexOption.IGNORE_CASE)
private val kiRegex = Regex("ki|channel|rage|wild", RegexOption.IGNORE_CASE)
private val ammoRegex = Regex("ammo|round", RegexOption.IGNORE_CASE)
private val edgeRegex = Regex("edge|luck", RegexOption.IGNORE_CASE)

private fun resourceDotColor(label: String): Color = when {
    spellRegex.containsMatchIn(label) -> ResourceSpell
    techRegex.containsMatchIn(label) -> ResourceTech
    kiRegex.containsMatchIn(label) -> ResourceKi
    ammoRegex.containsMatchIn(label) -> ResourceAmmo
    edgeRegex.containsMatchIn(label) -> ResourceEdge
    else -> ResourceDefault
}

private fun rsTier(score: Int): Pair<String, String> = when {
    score >= 95 -> "T7: Ride-or-Die" to "+5 all checks; fight together; share all"
    score >= 85 -> "T6: Ally" to "+4 CHA checks; auto-success Persuasion; backup"
    score >= 70 -> "T5: Close" to "+3 CHA checks; Adv Persuasion/Deception"
    score >= 55 -> "T4: Good" to "+2 CHA checks; Adv Persuasion"
    score >= 40 -> "T3: Friend" to "+2 Persuasion/Insight; request favors no roll"
    score >= 25 -> "T2: Friendly" to "+1 Persuasion"
    score >= 10 -> "T1: Acquaintance" to "no social penalties"
    score >= -9 -> "Neutral" to ""
    score >= -24 -> "-T1: Annoyed" to "Disadv Persuasion"
    score >= -39 -> "-T2: Disliked" to "-2 CHA checks"
    score >= -54 -> "-T3: Enemy" to "-3 CHA checks; passive sabotage"
    score >= -69 -> "-T4: Adversary" to "-4 all checks; 1 obstacle/episode"
    score >= -84 -> "-T5: Nemesis" to "-5 all checks; 2 complications/episode"
    score >= -94 -> "-T6: Sworn Enemy" to "-6 all checks; ambushes"
    else -> "-T7: Hatred" to "-7 all checks; attack regardless"
}

private fun romsTier(score: Int): Pair<String, String> = when {
    score >= 95 -> "T6: Unbreakable" to "+6 all checks; redirect dmg 1/LR; telepathy"
    score >= 85 -> "T5: Married" to "+5 all checks; shared HP pool; Adv Fear/Charm"
    score >= 65 -> "T4: Engaged" to "+4 all checks; gain 1 NPC skill; 1 Inspiration/ep"
    score >= 45 -> "T3: Partner" to "+3 CHA; fight together; reroll 1 save/LR"
    score >= 25 -> "T2: Dating" to "+2 Persuasion; Adv Insight; +1 Death saves"
    score >= 10 -> "T1: Flirting" to "+1 Persuasion; receptive to advances"
    else -> "None" to ""
}

private fun rsColor(score: Int): Color = when {
    score >= 40 -> RelationshipPurple
    score >= 10 -> RelationshipGreen
    score >= -9 -> RelationshipGray
    else -> RelationshipRed
}

private fun memoryBorderColor(impact: Int): Color = when {
    impact >= 4 -> MemoryHigh
    impact >= 3 -> MemoryMid
    else -> MemoryLow
}

// ─── Main CharacterPanel ───

@Composable
fun CharacterPanel(
    pipelineState: PipelineState,
    gameSystem: String?,
    characterSheetFiles: List<CharacterSheetFile>?,
    onFetchCharacterSheet: () -> Unit,
    modifier: Modifier = Modifier
) {
    val scene = pipelineState.sceneState
    val sceneNames = scene.pcsPresent + scene.npcsPresent
    val characterStates = pipelineState.characterStates
    val combat = pipelineState.combat
    // Use scene lists if populated, otherwise fall back to characterStates keys
    val allInScene = sceneNames.ifEmpty { characterStates.keys.toList() }
    val charCount = allInScene.size

    if (charCount == 0) return

    var expanded by remember { mutableStateOf(false) }
    var selectedCharName by remember { mutableStateOf<String?>(null) }
    var showAllChars by remember { mutableStateOf(false) }
    var showCallbacks by remember { mutableStateOf(false) }
    var showMemories by remember { mutableStateOf<String?>(null) }

    val configuration = LocalConfiguration.current
    val screenHeight = configuration.screenHeightDp.dp
    val expandedHeight = screenHeight * 0.55f
    val collapsedHeight = 34.dp
    val sheetHeight by animateDpAsState(
        targetValue = if (expanded) expandedHeight else collapsedHeight,
        animationSpec = tween(250),
        label = "sheet_height"
    )

    Box(modifier = modifier.fillMaxSize()) {
        // Backdrop overlay — covers full screen area above the sheet
        AnimatedVisibility(
            visible = expanded,
            enter = fadeIn(tween(250)),
            exit = fadeOut(tween(250))
        ) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color.Black.copy(alpha = 0.3f))
                    .clickable { expanded = false }
            )
        }

        // Bottom sheet — consumes clicks so they don't pass through to backdrop
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .height(sheetHeight)
                .align(Alignment.BottomCenter)
                .clip(RoundedCornerShape(topStart = 12.dp, topEnd = 12.dp))
                .background(CharPanelBg)
                .clickable(
                    interactionSource = remember { MutableInteractionSource() },
                    indication = null
                ) { /* consume clicks */ }
        ) {
            // Handle bar
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(collapsedHeight)
                    .clickable { expanded = !expanded }
                    .padding(horizontal = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    // Drag indicator bar
                    Box(
                        modifier = Modifier
                            .width(32.dp)
                            .height(3.dp)
                            .clip(RoundedCornerShape(2.dp))
                            .background(TextMuted.copy(alpha = 0.5f))
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    Icon(
                        imageVector = if (expanded) Icons.Filled.KeyboardArrowDown else Icons.Filled.KeyboardArrowUp,
                        contentDescription = if (expanded) "Collapse" else "Expand",
                        tint = TextMuted,
                        modifier = Modifier.size(18.dp)
                    )
                }
                Text(
                    text = if (combat != null) "Combat Round ${combat.round}" else "$charCount characters in scene",
                    color = TextMuted,
                    fontSize = 12.sp
                )
            }

            if (expanded) {
                // Character list
                val orderedNames = if (combat != null) {
                    combat.initiativeOrder.ifEmpty { allInScene }
                } else {
                    allInScene
                }

                LazyColumn(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth()
                        .padding(horizontal = 8.dp)
                ) {
                    if (combat != null && orderedNames.isNotEmpty()) {
                        item {
                            Text(
                                text = "INITIATIVE ORDER \u2022 Round ${combat.round}",
                                color = TextMuted,
                                fontSize = 10.sp,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.padding(start = 4.dp, bottom = 4.dp)
                            )
                        }
                    } else {
                        if (scene.pcsPresent.isNotEmpty()) {
                            item {
                                Text(
                                    text = "PCS",
                                    color = TextMuted,
                                    fontSize = 10.sp,
                                    fontWeight = FontWeight.Bold,
                                    modifier = Modifier.padding(start = 4.dp, bottom = 4.dp)
                                )
                            }
                        }
                    }

                    items(orderedNames.distinct(), key = { it }) { name ->
                        val cs = characterStates[name]
                        val data = cs?.data ?: CharacterData()
                        val isActive = combat?.currentTurn == name
                        CharacterCard(
                            name = name,
                            data = data,
                            isActiveTurn = isActive,
                            onClick = { selectedCharName = name }
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                    }
                }

                // Footer buttons
                SheetFooter(
                    openCallbackCount = pipelineState.callbackLedger.open.size,
                    totalCharCount = characterStates.size,
                    onCallbacksClick = { showCallbacks = true },
                    onViewAllClick = { showAllChars = true }
                )
            }
        }
    }

    // ── Modals ──

    selectedCharName?.let { name ->
        val cs = characterStates[name]
        val data = cs?.data ?: CharacterData()
        CharacterSheetModal(
            characterName = name,
            characterData = data,
            pipelineState = pipelineState,
            gameSystem = gameSystem,
            characterSheetFiles = characterSheetFiles,
            onDismiss = { selectedCharName = null },
            onViewMemories = { showMemories = name; selectedCharName = null },
            onFetchCharacterSheet = onFetchCharacterSheet
        )
    }

    if (showAllChars) {
        AllCharactersModal(
            pipelineState = pipelineState,
            onDismiss = { showAllChars = false },
            onSelectCharacter = { name -> showAllChars = false; selectedCharName = name }
        )
    }

    if (showCallbacks) {
        CallbacksModal(
            ledger = pipelineState.callbackLedger,
            turnCounter = pipelineState.turnCounter,
            onDismiss = { showCallbacks = false }
        )
    }

    showMemories?.let { name ->
        val memories = pipelineState.npcMemories[name] ?: emptyList()
        NpcMemoriesModal(
            characterName = name,
            memories = memories,
            onDismiss = { showMemories = null }
        )
    }
}

// ─── Sheet Footer ───

@Composable
private fun SheetFooter(
    openCallbackCount: Int,
    totalCharCount: Int,
    onCallbacksClick: () -> Unit,
    onViewAllClick: () -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        if (openCallbackCount > 0) {
            Text(
                text = "Callbacks ($openCallbackCount open)",
                color = CallbackOpen,
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                modifier = Modifier
                    .clip(RoundedCornerShape(4.dp))
                    .background(CallbackOpen.copy(alpha = 0.15f))
                    .clickable { onCallbacksClick() }
                    .padding(horizontal = 8.dp, vertical = 4.dp)
            )
        } else {
            Spacer(modifier = Modifier.width(1.dp))
        }

        if (totalCharCount > 0) {
            Text(
                text = "View All ($totalCharCount)",
                color = TextSecondary,
                fontSize = 12.sp,
                modifier = Modifier
                    .clip(RoundedCornerShape(4.dp))
                    .background(CharPanelCardBg)
                    .clickable { onViewAllClick() }
                    .padding(horizontal = 8.dp, vertical = 4.dp)
            )
        }
    }
}

// ─── Character Card ───

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CharacterCard(
    name: String,
    data: CharacterData,
    isActiveTurn: Boolean,
    onClick: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .border(
                width = 1.dp,
                color = if (isActiveTurn) CallbackOpen else CharPanelCardBorder,
                shape = RoundedCornerShape(8.dp)
            )
            .background(CharPanelCardBg)
            .clickable { onClick() }
            .padding(8.dp)
    ) {
        // Header: name + ACTING + type badge
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.weight(1f)
            ) {
                Text(
                    text = name,
                    color = TextPrimary,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f, fill = false)
                )
                if (isActiveTurn) {
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = "ACTING",
                        color = CallbackOpen,
                        fontSize = 9.sp,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier
                            .clip(RoundedCornerShape(3.dp))
                            .background(CallbackOpen.copy(alpha = 0.2f))
                            .padding(horizontal = 4.dp, vertical = 1.dp)
                    )
                }
            }
            data.type?.let { type ->
                TypeBadge(type)
            }
        }

        // Class/level row + flat vitals
        val classLevel = buildString {
            data.charClass?.let { append(it) }
            data.level?.let {
                if (isNotEmpty()) append(" ")
                append("Lv${it.toCleanString()}")
            }
        }
        val flatVitals = data.vitals.filter { it.isFlat }
        if (classLevel.isNotEmpty() || flatVitals.isNotEmpty()) {
            Spacer(modifier = Modifier.height(2.dp))
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                if (classLevel.isNotEmpty()) {
                    Text(text = classLevel, color = TextSecondary, fontSize = 11.sp)
                }
                flatVitals.forEach { vital ->
                    Text(
                        text = "${vital.label}: ${vital.displayValue}",
                        color = TextMuted,
                        fontSize = 10.sp,
                        modifier = Modifier
                            .clip(RoundedCornerShape(3.dp))
                            .background(CharPanelCardBorder.copy(alpha = 0.5f))
                            .padding(horizontal = 4.dp, vertical = 1.dp)
                    )
                }
            }
        }

        // Bar vitals
        val barVitals = data.vitals.filter { !it.isFlat && it.current != null && it.max != null }
        barVitals.forEach { vital ->
            Spacer(modifier = Modifier.height(3.dp))
            VitalBarRow(vital)
        }

        // Summary fallback
        if (barVitals.isEmpty() && classLevel.isEmpty() && !data.summary.isNullOrBlank()) {
            Spacer(modifier = Modifier.height(2.dp))
            Text(
                text = data.summary,
                color = TextSecondary,
                fontSize = 11.sp,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
        }

        // Resource dots (first 2)
        val resources = data.resources.take(2)
        if (resources.isNotEmpty()) {
            Spacer(modifier = Modifier.height(3.dp))
            resources.forEach { res ->
                ResourceDotsRow(res)
            }
        }

        // Condition pills
        if (data.conditions.isNotEmpty()) {
            Spacer(modifier = Modifier.height(3.dp))
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(2.dp)
            ) {
                data.conditions.forEach { cond ->
                    val color = conditionColor(cond)
                    Text(
                        text = cond,
                        color = color,
                        fontSize = 9.sp,
                        modifier = Modifier
                            .clip(RoundedCornerShape(3.dp))
                            .background(color.copy(alpha = 0.15f))
                            .padding(horizontal = 4.dp, vertical = 1.dp)
                    )
                }
            }
        }
    }
}

@Composable
private fun TypeBadge(type: String) {
    val color = typeBadgeColor(type)
    Text(
        text = type.uppercase(),
        color = color,
        fontSize = 9.sp,
        fontWeight = FontWeight.SemiBold,
        modifier = Modifier
            .clip(RoundedCornerShape(3.dp))
            .background(color.copy(alpha = 0.15f))
            .padding(horizontal = 5.dp, vertical = 1.dp)
    )
}

@Composable
private fun VitalBarRow(vital: VitalBar) {
    val cur = vital.current ?: 0.0
    val mx = vital.max ?: 1.0
    val color = vitalColor(cur, mx, vital.label)
    val progress = if (mx > 0.0) (cur / mx).toFloat().coerceIn(0f, 1f) else 0f

    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth()
    ) {
        Text(
            text = vital.label,
            color = TextMuted,
            fontSize = 10.sp,
            modifier = Modifier.width(40.dp)
        )
        LinearProgressIndicator(
            progress = { progress },
            modifier = Modifier
                .weight(1f)
                .height(4.dp)
                .clip(RoundedCornerShape(2.dp)),
            color = color,
            trackColor = CharPanelCardBorder,
        )
        Spacer(modifier = Modifier.width(4.dp))
        Text(
            text = "$cur/$mx",
            color = TextSecondary,
            fontSize = 10.sp
        )
    }
}

@Composable
private fun ResourceDotsRow(resource: ResourceEntry) {
    val color = resourceDotColor(resource.label)
    val cur = resource.current.toInt().coerceIn(0, 20)
    val mx = resource.max.toInt().coerceIn(0, 20)
    val dots = buildString {
        repeat(cur) { append("\u25CF") }
        repeat((mx - cur).coerceAtLeast(0)) { append("\u25CB") }
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(text = resource.label, color = TextMuted, fontSize = 10.sp)
        Spacer(modifier = Modifier.width(4.dp))
        Text(text = dots, color = color, fontSize = 8.sp, letterSpacing = 1.sp)
    }
}

// ─── Character Sheet Modal ───

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CharacterSheetModal(
    characterName: String,
    characterData: CharacterData,
    pipelineState: PipelineState,
    gameSystem: String?,
    characterSheetFiles: List<CharacterSheetFile>?,
    onDismiss: () -> Unit,
    onViewMemories: () -> Unit,
    onFetchCharacterSheet: () -> Unit
) {
    val memories = pipelineState.npcMemories[characterName] ?: emptyList()
    var sheetExpanded by remember { mutableStateOf(false) }

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .heightIn(max = LocalConfiguration.current.screenHeightDp.dp * 0.85f)
                .clip(RoundedCornerShape(12.dp))
                .background(CharPanelBg)
                .border(1.dp, CharPanelCardBorder, RoundedCornerShape(12.dp))
                .verticalScroll(rememberScrollState())
                .padding(16.dp)
        ) {
            // Header
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.weight(1f)) {
                    Text(
                        text = characterName,
                        color = TextPrimary,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f, fill = false)
                    )
                    characterData.type?.let { type ->
                        Spacer(modifier = Modifier.width(8.dp))
                        TypeBadge(type)
                    }
                }
                IconButton(onClick = onDismiss, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Filled.Close, "Close", tint = TextMuted, modifier = Modifier.size(20.dp))
                }
            }

            // Class/Level
            val classLevel = buildString {
                characterData.charClass?.let { append(it) }
                characterData.level?.let {
                    if (isNotEmpty()) append(" \u2022 ")
                    append("Level ${it.toCleanString()}")
                }
            }
            if (classLevel.isNotEmpty()) {
                Spacer(modifier = Modifier.height(4.dp))
                Text(text = classLevel, color = TextSecondary, fontSize = 13.sp)
            }

            Spacer(modifier = Modifier.height(12.dp))

            // Live State: All vitals as bars
            val barVitals = characterData.vitals.filter { !it.isFlat && it.current != null && it.max != null }
            val flatVitals = characterData.vitals.filter { it.isFlat }

            if (barVitals.isNotEmpty()) {
                SectionLabel("VITALS")
                barVitals.forEach { vital ->
                    VitalBarRow(vital)
                    Spacer(modifier = Modifier.height(4.dp))
                }
            }

            if (flatVitals.isNotEmpty()) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    flatVitals.forEach { vital ->
                        Text(
                            text = "${vital.label}: ${vital.displayValue}",
                            color = TextSecondary,
                            fontSize = 12.sp,
                            modifier = Modifier
                                .clip(RoundedCornerShape(4.dp))
                                .background(CharPanelCardBg)
                                .padding(horizontal = 6.dp, vertical = 2.dp)
                        )
                    }
                }
                Spacer(modifier = Modifier.height(8.dp))
            }

            // Resources as bars
            if (characterData.resources.isNotEmpty()) {
                SectionLabel("RESOURCES")
                characterData.resources.forEach { res ->
                    ResourceBarRow(res)
                    Spacer(modifier = Modifier.height(4.dp))
                }
            }

            // Conditions
            if (characterData.conditions.isNotEmpty()) {
                SectionLabel("CONDITIONS")
                FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    characterData.conditions.forEach { cond ->
                        val color = conditionColor(cond)
                        Text(
                            text = cond,
                            color = color,
                            fontSize = 11.sp,
                            modifier = Modifier
                                .clip(RoundedCornerShape(4.dp))
                                .background(color.copy(alpha = 0.15f))
                                .padding(horizontal = 6.dp, vertical = 2.dp)
                        )
                    }
                }
                Spacer(modifier = Modifier.height(8.dp))
            }

            // Summary
            if (!characterData.summary.isNullOrBlank()) {
                Text(text = characterData.summary, color = TextSecondary, fontSize = 12.sp)
                Spacer(modifier = Modifier.height(8.dp))
            }

            // Game-specific state
            GameSpecificState(
                characterName = characterName,
                characterData = characterData,
                pipelineState = pipelineState,
                gameSystem = gameSystem
            )

            // Funds / Credits
            FundsSection(characterName, characterData, pipelineState, gameSystem)

            // NPC Memories button
            if (memories.isNotEmpty()) {
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    text = "View Memories (${memories.size})",
                    color = ResourceDefault,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium,
                    modifier = Modifier
                        .clip(RoundedCornerShape(4.dp))
                        .background(ResourceDefault.copy(alpha = 0.15f))
                        .clickable { onViewMemories() }
                        .padding(horizontal = 8.dp, vertical = 4.dp)
                )
            }

            // Collapsible character sheet
            Spacer(modifier = Modifier.height(12.dp))
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(4.dp))
                    .background(CharPanelCardBg)
                    .clickable {
                        sheetExpanded = !sheetExpanded
                        if (sheetExpanded && characterSheetFiles == null) onFetchCharacterSheet()
                    }
                    .padding(horizontal = 8.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    imageVector = if (sheetExpanded) Icons.Filled.KeyboardArrowUp else Icons.Filled.KeyboardArrowDown,
                    contentDescription = "Toggle sheet",
                    tint = TextMuted,
                    modifier = Modifier.size(16.dp)
                )
                Spacer(modifier = Modifier.width(4.dp))
                Text("Character Sheet", color = TextSecondary, fontSize = 12.sp)
            }

            if (sheetExpanded) {
                Spacer(modifier = Modifier.height(4.dp))
                val files = characterSheetFiles
                if (files == null) {
                    Text(
                        text = "Loading...",
                        color = TextMuted,
                        fontSize = 11.sp,
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(4.dp))
                            .background(CharPanelCardBg)
                            .padding(8.dp)
                    )
                } else {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(4.dp))
                            .background(CharPanelCardBg)
                            .padding(8.dp)
                    ) {
                        files.forEachIndexed { index, file ->
                            val ext = file.name.substringAfterLast('.', "").lowercase()
                            if (ext == "yaml" || ext == "yml") {
                                YamlHighlighted(file.content)
                            } else {
                                SheetMarkdown(file.content)
                            }
                            if (index < files.lastIndex) {
                                Spacer(modifier = Modifier.height(12.dp))
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(
        text = text,
        color = TextMuted,
        fontSize = 10.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = 1.sp,
        modifier = Modifier.padding(bottom = 4.dp)
    )
}

@Composable
private fun ResourceBarRow(resource: ResourceEntry) {
    val color = resourceDotColor(resource.label)
    val progress = if (resource.max > 0.0) (resource.current / resource.max).toFloat().coerceIn(0f, 1f) else 0f

    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth()
    ) {
        Text(
            text = resource.label,
            color = TextMuted,
            fontSize = 11.sp,
            modifier = Modifier.width(80.dp)
        )
        LinearProgressIndicator(
            progress = { progress },
            modifier = Modifier
                .weight(1f)
                .height(4.dp)
                .clip(RoundedCornerShape(2.dp)),
            color = color,
            trackColor = CharPanelCardBorder,
        )
        Spacer(modifier = Modifier.width(4.dp))
        Text(
            text = "${resource.current.toCleanString()}/${resource.max.toCleanString()}",
            color = TextSecondary,
            fontSize = 10.sp
        )
    }
}

// ─── Game-Specific State ───

@Composable
private fun GameSpecificState(
    characterName: String,
    characterData: CharacterData,
    pipelineState: PipelineState,
    gameSystem: String?
) {
    val gs = pipelineState.gameState
    val type = characterData.type?.lowercase()

    when (gameSystem?.lowercase()) {
        "dnd5e", "dnd5e_cyber" -> DnD5eState(characterName, characterData, gs, gameSystem)
        "coc7e" -> CoC7eState(characterName, gs)
        "sr6e" -> Sr6eState(characterName, gs)
        "cpred" -> CpRedState(characterName, gs)
    }
}

@Suppress("UNCHECKED_CAST")
@Composable
private fun DnD5eState(
    characterName: String,
    characterData: CharacterData,
    gameState: Map<String, Any>,
    gameSystem: String?
) {
    val type = characterData.type?.lowercase()
    val relationships = gameState["relationships"] as? Map<String, Any> ?: emptyMap()
    val factions = gameState["factions"] as? Map<String, Any> ?: emptyMap()

    // Relationships
    if (type == "pc" && relationships.isNotEmpty()) {
        SectionLabel("RELATIONSHIPS")
        relationships.forEach { (npcName, data) ->
            val relMap = data as? Map<String, Any> ?: return@forEach
            val rs = (relMap["rs"] as? Number)?.toInt() ?: return@forEach
            val roms = (relMap["roms"] as? Number)?.toInt()
            val (rsTierLabel, rsBonus) = rsTier(rs)
            val color = rsColor(rs)

            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 2.dp),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(text = npcName, color = TextSecondary, fontSize = 11.sp)
                Column(horizontalAlignment = Alignment.End) {
                    Text(
                        text = "RS $rs \u2014 $rsTierLabel",
                        color = color,
                        fontSize = 10.sp
                    )
                    if (rsBonus.isNotEmpty()) {
                        Text(text = rsBonus, color = TextMuted, fontSize = 9.sp)
                    }
                    if (roms != null && roms >= 10) {
                        val (romsTierLabel, romsBonus) = romsTier(roms)
                        Text(
                            text = "ROMS $roms \u2014 $romsTierLabel",
                            color = RelationshipPink,
                            fontSize = 10.sp
                        )
                        if (romsBonus.isNotEmpty()) {
                            Text(text = romsBonus, color = TextMuted, fontSize = 9.sp)
                        }
                    }
                }
            }
        }
        Spacer(modifier = Modifier.height(8.dp))
    } else if (type == "npc" && relationships.isNotEmpty()) {
        // NPC: show this NPC's RS from each PC's perspective
        val npcRel = relationships[characterName] as? Map<String, Any>
        if (npcRel != null) {
            SectionLabel("RELATIONSHIP")
            val rs = (npcRel["rs"] as? Number)?.toInt()
            val roms = (npcRel["roms"] as? Number)?.toInt()
            if (rs != null) {
                val (rsTierLabel, rsBonus) = rsTier(rs)
                Text(text = "RS $rs \u2014 $rsTierLabel", color = rsColor(rs), fontSize = 11.sp)
                if (rsBonus.isNotEmpty()) {
                    Text(text = rsBonus, color = TextMuted, fontSize = 9.sp)
                }
            }
            if (roms != null && roms >= 10) {
                val (romsTierLabel, romsBonus) = romsTier(roms)
                Text(text = "ROMS $roms \u2014 $romsTierLabel", color = RelationshipPink, fontSize = 11.sp)
                if (romsBonus.isNotEmpty()) {
                    Text(text = romsBonus, color = TextMuted, fontSize = 9.sp)
                }
            }
            Spacer(modifier = Modifier.height(8.dp))
        }
    }

    // Factions (PC only)
    if (type == "pc" && factions.isNotEmpty()) {
        SectionLabel("FACTIONS")
        factions.forEach { (factionName, data) ->
            val facMap = data as? Map<String, Any> ?: return@forEach
            val fr = (facMap["fr"] as? Number)?.toInt() ?: return@forEach
            Text(
                text = "$factionName: FR $fr",
                color = TextSecondary,
                fontSize = 11.sp,
                modifier = Modifier.padding(vertical = 1.dp)
            )
        }
        Spacer(modifier = Modifier.height(8.dp))
    }

    // Ship hull/shields for dnd5e_cyber
    if (gameSystem?.lowercase() == "dnd5e_cyber" && type == "ship") {
        val ship = gameState["ship"] as? Map<String, Any>
        if (ship != null) {
            val hull = ship["hull"] as? Map<String, Number>
            val shields = ship["shields"] as? Map<String, Number>
            SectionLabel("SHIP SYSTEMS")
            hull?.let {
                val cur = it["current"]?.toDouble() ?: 0.0
                val mx = it["max"]?.toDouble() ?: 1.0
                VitalBarRow(VitalBar("Hull", cur, mx))
                Spacer(modifier = Modifier.height(4.dp))
            }
            shields?.let {
                val cur = it["current"]?.toDouble() ?: 0.0
                val mx = it["max"]?.toDouble() ?: 1.0
                VitalBarRow(VitalBar("Shield", cur, mx))
                Spacer(modifier = Modifier.height(4.dp))
            }
            Spacer(modifier = Modifier.height(4.dp))
        }
    }
}

@Suppress("UNCHECKED_CAST")
@Composable
private fun CoC7eState(characterName: String, gameState: Map<String, Any>) {
    val investigators = gameState["investigators"] as? Map<String, Any> ?: return
    val inv = investigators[characterName] as? Map<String, Any> ?: return

    SectionLabel("INVESTIGATOR STATE")

    // SAN bar
    val san = inv["san"] as? Map<String, Number>
    san?.let {
        val cur = it["current"]?.toDouble() ?: 0.0
        val mx = it["max"]?.toDouble() ?: 99.0
        VitalBarRow(VitalBar("SAN", cur, mx))
        Spacer(modifier = Modifier.height(4.dp))
    }

    // Luck
    val luck = (inv["luck"] as? Number)?.toInt()
    luck?.let {
        Text(text = "Luck: $it", color = VitalYellow, fontSize = 12.sp)
        Spacer(modifier = Modifier.height(2.dp))
    }

    // Cthulhu Mythos
    val mythos = (inv["mythos_pct"] as? Number)?.toInt() ?: (inv["mythos"] as? Number)?.toInt()
    mythos?.let {
        Text(text = "Cthulhu Mythos: $it%", color = VitalSanMid, fontSize = 12.sp)
        Spacer(modifier = Modifier.height(2.dp))
    }

    // Bonds
    val bonds = inv["bonds"] as? Map<String, Any>
    if (!bonds.isNullOrEmpty()) {
        Spacer(modifier = Modifier.height(4.dp))
        Text(text = "Bonds:", color = TextMuted, fontSize = 11.sp, fontWeight = FontWeight.Medium)
        bonds.forEach { (name, value) ->
            Text(text = "  $name: $value", color = TextSecondary, fontSize = 11.sp)
        }
    }

    // Phobias
    val phobias = inv["phobias"] as? List<String>
    if (!phobias.isNullOrEmpty()) {
        Spacer(modifier = Modifier.height(4.dp))
        Text(
            text = "Phobias: ${phobias.joinToString(", ")}",
            color = ConditionDanger,
            fontSize = 11.sp
        )
    }

    // Manias
    val manias = inv["manias"] as? List<String>
    if (!manias.isNullOrEmpty()) {
        Spacer(modifier = Modifier.height(2.dp))
        Text(
            text = "Manias: ${manias.joinToString(", ")}",
            color = ConditionWarning,
            fontSize = 11.sp
        )
    }

    Spacer(modifier = Modifier.height(8.dp))
}

@Suppress("UNCHECKED_CAST")
@Composable
private fun Sr6eState(characterName: String, gameState: Map<String, Any>) {
    val runners = gameState["runners"] as? Map<String, Any> ?: return
    val runner = runners[characterName] as? Map<String, Any> ?: return

    SectionLabel("RUNNER STATE")

    // Edge
    val edge = runner["edge"] as? Map<String, Number>
    edge?.let {
        val cur = it["current"]?.toDouble() ?: 0.0
        val mx = it["max"]?.toDouble() ?: 0.0
        ResourceDotsRow(ResourceEntry("Edge", cur, mx))
        Spacer(modifier = Modifier.height(4.dp))
    }

    // Essence
    val essence = (runner["essence"] as? Number)?.toFloat()
    essence?.let {
        Text(text = "Essence: $it", color = VitalSanMid, fontSize = 12.sp)
        Spacer(modifier = Modifier.height(2.dp))
    }

    // Nuyen
    val nuyen = runner["nuyen"]
    nuyen?.let {
        Text(text = "\u00A5$it", color = VitalYellow, fontSize = 12.sp)
    }

    Spacer(modifier = Modifier.height(8.dp))
}

@Suppress("UNCHECKED_CAST")
@Composable
private fun CpRedState(characterName: String, gameState: Map<String, Any>) {
    val edgerunners = gameState["edgerunners"] as? Map<String, Any> ?: return
    val er = edgerunners[characterName] as? Map<String, Any> ?: return

    SectionLabel("EDGERUNNER STATE")

    // Humanity
    val humanity = er["humanity"] as? Map<String, Number>
    humanity?.let {
        val cur = it["current"]?.toDouble() ?: 0.0
        val mx = it["max"]?.toDouble() ?: 0.0
        VitalBarRow(VitalBar("Humanity", cur, mx))
        Spacer(modifier = Modifier.height(4.dp))
    }

    // Luck
    val luck = er["luck"] as? Map<String, Number>
    luck?.let {
        val cur = it["current"]?.toDouble() ?: 0.0
        val mx = it["max"]?.toDouble() ?: 0.0
        ResourceDotsRow(ResourceEntry("Luck", cur, mx))
        Spacer(modifier = Modifier.height(4.dp))
    }

    // Eurobucks
    val euro = er["eurobucks"]
    euro?.let {
        Text(text = "\u20AC$it", color = VitalYellow, fontSize = 12.sp)
        Spacer(modifier = Modifier.height(2.dp))
    }

    // Critical Injuries
    val injuries = er["critical_injuries"] as? List<String>
    if (!injuries.isNullOrEmpty()) {
        Spacer(modifier = Modifier.height(4.dp))
        Text(text = "Critical Injuries:", color = ConditionDanger, fontSize = 11.sp, fontWeight = FontWeight.Medium)
        injuries.forEach { inj ->
            Text(text = "\u2022 $inj", color = ConditionDanger, fontSize = 11.sp)
        }
    }

    Spacer(modifier = Modifier.height(8.dp))
}

// ─── Funds Section ───

@Suppress("UNCHECKED_CAST")
@Composable
private fun FundsSection(
    characterName: String,
    characterData: CharacterData,
    pipelineState: PipelineState,
    gameSystem: String?
) {
    val funds = pipelineState.hudState?.funds ?: return
    val type = characterData.type?.lowercase()
    val gs = gameSystem?.lowercase() ?: if (pipelineState.gameState.containsKey("ship")) "dnd5e_cyber" else "dnd5e"
    val label = when (gs) {
        "dnd5e_cyber" -> "CREDITS"
        "sr6e" -> "NUYEN"
        "cpred" -> "EUROBUCKS"
        else -> "FUNDS"
    }

    // Ships show all funds, others show own
    val displayFunds: Map<String, Any> = when (type) {
        "ship" -> funds
        else -> funds.filterKeys { it.equals(characterName, ignoreCase = true) }
    }

    if (displayFunds.isEmpty()) return

    SectionLabel(label)
    displayFunds.forEach { (name, value) ->
        Text(text = "$name: $value", color = VitalYellow, fontSize = 12.sp)
    }
    Spacer(modifier = Modifier.height(8.dp))
}

// ─── All Characters Modal ───

@Composable
private fun AllCharactersModal(
    pipelineState: PipelineState,
    onDismiss: () -> Unit,
    onSelectCharacter: (String) -> Unit
) {
    val scene = pipelineState.sceneState
    val inScene = (scene.pcsPresent + scene.npcsPresent).toSet()
    val allNames = (pipelineState.characterStates.keys + pipelineState.npcMemories.keys).distinct()
    val inSceneChars = allNames.filter { it in inScene }
    val otherChars = allNames.filter { it !in inScene }

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .heightIn(max = LocalConfiguration.current.screenHeightDp.dp * 0.85f)
                .clip(RoundedCornerShape(12.dp))
                .background(CharPanelBg)
                .border(1.dp, CharPanelCardBorder, RoundedCornerShape(12.dp))
                .padding(16.dp)
        ) {
            // Header
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = "All Characters (${allNames.size})",
                    color = TextPrimary,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold
                )
                IconButton(onClick = onDismiss, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Filled.Close, "Close", tint = TextMuted, modifier = Modifier.size(20.dp))
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                if (inSceneChars.isNotEmpty()) {
                    SectionLabel("IN SCENE")
                    inSceneChars.forEach { name ->
                        MiniCharCard(
                            name = name,
                            type = pipelineState.characterStates[name]?.data?.type,
                            memoryCount = pipelineState.npcMemories[name]?.size ?: 0,
                            onClick = { onSelectCharacter(name) }
                        )
                    }
                    Spacer(modifier = Modifier.height(8.dp))
                }

                if (otherChars.isNotEmpty()) {
                    SectionLabel("OTHER CHARACTERS")
                    otherChars.forEach { name ->
                        MiniCharCard(
                            name = name,
                            type = pipelineState.characterStates[name]?.data?.type,
                            memoryCount = pipelineState.npcMemories[name]?.size ?: 0,
                            onClick = { onSelectCharacter(name) }
                        )
                    }
                }

                if (allNames.isEmpty()) {
                    Text("No characters yet.", color = TextMuted, fontSize = 12.sp)
                }
            }
        }
    }
}

@Composable
private fun MiniCharCard(
    name: String,
    type: String?,
    memoryCount: Int,
    onClick: () -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(6.dp))
            .background(CharPanelCardBg)
            .clickable { onClick() }
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(
            text = name,
            color = TextPrimary,
            fontSize = 13.sp,
            modifier = Modifier.weight(1f)
        )
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            if (memoryCount > 0) {
                Text(text = "$memoryCount mem", color = TextMuted, fontSize = 10.sp)
            }
            type?.let { TypeBadge(it) }
        }
    }
    Spacer(modifier = Modifier.height(4.dp))
}

// ─── NPC Memories Modal ───

@Composable
private fun NpcMemoriesModal(
    characterName: String,
    memories: List<NpcMemoryEntry>,
    onDismiss: () -> Unit
) {
    val sorted = remember(memories) {
        memories.sortedWith(compareByDescending<NpcMemoryEntry> { it.impact }.thenByDescending { it.turnCreated })
    }

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .heightIn(max = LocalConfiguration.current.screenHeightDp.dp * 0.85f)
                .clip(RoundedCornerShape(12.dp))
                .background(CharPanelBg)
                .border(1.dp, CharPanelCardBorder, RoundedCornerShape(12.dp))
                .padding(16.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = "$characterName \u2014 Memories (${memories.size})",
                    color = TextPrimary,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f)
                )
                IconButton(onClick = onDismiss, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Filled.Close, "Close", tint = TextMuted, modifier = Modifier.size(20.dp))
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            if (sorted.isEmpty()) {
                Text("No memories.", color = TextMuted, fontSize = 12.sp)
            } else {
                LazyColumn(modifier = Modifier.weight(1f)) {
                    items(sorted) { mem ->
                        MemoryCard(mem)
                        Spacer(modifier = Modifier.height(6.dp))
                    }
                }
            }
        }
    }
}

@Composable
private fun MemoryCard(memory: NpcMemoryEntry) {
    val borderColor = memoryBorderColor(memory.impact)
    val stars = buildString {
        repeat(memory.impact.coerceIn(0, 5)) { append("\u2605") }
        repeat((5 - memory.impact).coerceIn(0, 5)) { append("\u2606") }
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(6.dp))
            .border(
                width = 1.dp,
                color = CharPanelCardBorder,
                shape = RoundedCornerShape(6.dp)
            )
            .background(CharPanelCardBg)
            .drawLeftBorder(borderColor, 3.dp)
            .padding(8.dp)
    ) {
        // Metadata row
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text(
                text = memory.date ?: "Turn ${memory.turnCreated}",
                color = TextMuted,
                fontSize = 10.sp
            )
            Text(text = stars, color = borderColor, fontSize = 10.sp)
        }

        Spacer(modifier = Modifier.height(4.dp))

        // Main text
        val displayText = memory.text ?: memory.memory ?: ""
        if (displayText.isNotEmpty()) {
            Text(text = displayText, color = TextPrimary, fontSize = 12.sp)
        }

        // Quote
        if (!memory.quote.isNullOrBlank()) {
            Spacer(modifier = Modifier.height(2.dp))
            Text(
                text = "\u201C${memory.quote}\u201D",
                color = TextMuted,
                fontSize = 11.sp,
                fontStyle = FontStyle.Italic
            )
        }
    }
}

// ─── Callbacks Modal ───

@Composable
private fun CallbacksModal(
    ledger: CallbackLedger,
    turnCounter: Int,
    onDismiss: () -> Unit
) {
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .heightIn(max = LocalConfiguration.current.screenHeightDp.dp * 0.85f)
                .clip(RoundedCornerShape(12.dp))
                .background(CharPanelBg)
                .border(1.dp, CharPanelCardBorder, RoundedCornerShape(12.dp))
                .padding(16.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = "Callbacks",
                    color = TextPrimary,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold
                )
                IconButton(onClick = onDismiss, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Filled.Close, "Close", tint = TextMuted, modifier = Modifier.size(20.dp))
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                if (ledger.open.isNotEmpty()) {
                    SectionLabel("OPEN (${ledger.open.size})")
                    ledger.open.forEach { cb ->
                        CallbackCard(cb, CallbackOpen, turnCounter)
                        Spacer(modifier = Modifier.height(6.dp))
                    }
                }

                if (ledger.recentlyResolved.isNotEmpty()) {
                    Spacer(modifier = Modifier.height(8.dp))
                    SectionLabel("RECENTLY RESOLVED (${ledger.recentlyResolved.size})")
                    ledger.recentlyResolved.forEach { cb ->
                        CallbackCard(cb, CallbackResolved, turnCounter)
                        Spacer(modifier = Modifier.height(6.dp))
                    }
                }

                if (ledger.open.isEmpty() && ledger.recentlyResolved.isEmpty()) {
                    Text("No callbacks.", color = TextMuted, fontSize = 12.sp)
                }
            }
        }
    }
}

@Composable
private fun CallbackCard(callback: CallbackEntry, borderColor: Color, turnCounter: Int) {
    val age = turnCounter - callback.createdTurn
    val isAging = age >= 20

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(6.dp))
            .border(1.dp, CharPanelCardBorder, RoundedCornerShape(6.dp))
            .background(CharPanelCardBg)
            .drawLeftBorder(borderColor, 3.dp)
            .padding(8.dp)
    ) {
        // Source NPC
        callback.sourceNpc?.let { npc ->
            Text(text = npc, color = TextMuted, fontSize = 10.sp)
            Spacer(modifier = Modifier.height(2.dp))
        }

        // Description / original text
        val text = callback.originalText ?: callback.description ?: ""
        if (text.isNotEmpty()) {
            Text(text = text, color = TextPrimary, fontSize = 12.sp)
        }

        // Metadata
        Spacer(modifier = Modifier.height(4.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                text = "Turn ${callback.createdTurn}",
                color = TextMuted,
                fontSize = 10.sp
            )
            callback.resolvedTurn?.let {
                Text(text = "Resolved turn $it", color = TextMuted, fontSize = 10.sp)
            }
            if (isAging && callback.resolvedTurn == null) {
                Text(
                    text = "\u26A0 aging ($age turns)",
                    color = ConditionWarning,
                    fontSize = 10.sp
                )
            }
        }
    }
}

// ─── Sheet Markdown ───

/** Renders character sheet content as formatted markdown with syntax-highlighted code blocks. */
@Composable
private fun SheetMarkdown(content: String) {
    val colors = markdownColor(
        text = TextSecondary,
        codeText = TextSecondary,
        inlineCodeText = TextSecondary,
        linkText = Accent,
        codeBackground = CharPanelBg,
        inlineCodeBackground = CharPanelBg,
        dividerColor = CharPanelCardBorder
    )
    val typography = markdownTypography(
        h1 = MaterialTheme.typography.titleMedium.copy(color = TextPrimary),
        h2 = MaterialTheme.typography.titleSmall.copy(color = TextPrimary),
        h3 = MaterialTheme.typography.bodyLarge.copy(color = TextPrimary, fontWeight = FontWeight.Bold),
        h4 = MaterialTheme.typography.bodyMedium.copy(color = TextPrimary, fontWeight = FontWeight.Bold),
        h5 = MaterialTheme.typography.bodySmall.copy(color = TextPrimary, fontWeight = FontWeight.Bold),
        h6 = MaterialTheme.typography.bodySmall.copy(color = TextSecondary, fontWeight = FontWeight.Bold),
        text = MaterialTheme.typography.bodySmall.copy(color = TextSecondary),
        code = MaterialTheme.typography.bodySmall.copy(
            fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
            color = TextSecondary
        ),
        paragraph = MaterialTheme.typography.bodySmall.copy(color = TextSecondary)
    )

    Markdown(
        content = content,
        colors = colors,
        typography = typography,
        components = markdownComponents(
            codeBlock = highlightedCodeBlock,
            codeFence = highlightedCodeFence,
        ),
        modifier = Modifier.fillMaxWidth()
    )
}

// ─── YAML Syntax Highlighting (VS Code Dark+ palette) ───

private val YamlKey = Color(0xFF9CDCFE)       // light blue — mapping keys
private val YamlString = Color(0xFFCE9178)     // orange-brown — quoted strings
private val YamlBool = Color(0xFF569CD6)       // blue — true/false/null
private val YamlNumber = Color(0xFFB5CEA8)     // light green — numeric literals
private val YamlComment = Color(0xFF6A9955)    // green — comments
private val YamlPunct = Color(0xFFD4D4D4)      // gray — colons, dashes, brackets
private val YamlText = Color(0xFFD4D4D4)       // gray — default/unquoted values
private val YamlAnchor = Color(0xFFDCDCAA)     // yellow — anchors & aliases

private val yamlBoolPattern = Regex("^(true|false|yes|no|on|off|null|~)$", RegexOption.IGNORE_CASE)
private val yamlNumberPattern = Regex("^[+-]?(\\d[\\d_]*(\\.[\\d_]+)?([eE][+-]?\\d+)?|0x[0-9a-fA-F]+|0o[0-7]+|\\.inf|\\.nan)$")

/** Renders YAML content with VS Code Dark+ syntax highlighting. */
@Composable
private fun YamlHighlighted(content: String) {
    val annotated = remember(content) { highlightYaml(content) }
    Text(
        text = annotated,
        fontFamily = FontFamily.Monospace,
        fontSize = 12.sp,
        lineHeight = 18.sp,
        modifier = Modifier
            .fillMaxWidth()
            .background(CharPanelBg, RoundedCornerShape(4.dp))
            .padding(8.dp)
    )
}

private fun highlightYaml(content: String) = buildAnnotatedString {
    val defaultStyle = SpanStyle(color = YamlText)
    content.lines().forEachIndexed { lineIdx, line ->
        if (lineIdx > 0) append('\n')
        if (line.isBlank()) {
            append(line)
            return@forEachIndexed
        }
        // Comment line (possibly indented)
        val trimmed = line.trimStart()
        if (trimmed.startsWith('#')) {
            val indent = line.length - trimmed.length
            withStyle(defaultStyle) { append(line.substring(0, indent)) }
            withStyle(SpanStyle(color = YamlComment)) { append(trimmed) }
            return@forEachIndexed
        }
        // Document markers
        if (trimmed == "---" || trimmed == "...") {
            withStyle(SpanStyle(color = YamlPunct)) { append(line) }
            return@forEachIndexed
        }
        // Process key: value lines and list items
        highlightYamlLine(line, defaultStyle)
    }
}

private fun AnnotatedString.Builder.highlightYamlLine(line: String, defaultStyle: SpanStyle) {
    var pos = 0
    val len = line.length

    // Leading whitespace
    while (pos < len && line[pos] == ' ') pos++
    if (pos > 0) withStyle(defaultStyle) { append(line.substring(0, pos)) }

    if (pos >= len) return

    // List item dash
    if (line[pos] == '-' && (pos + 1 >= len || line[pos + 1] == ' ')) {
        withStyle(SpanStyle(color = YamlPunct)) { append("-") }
        pos++
        if (pos < len && line[pos] == ' ') {
            withStyle(defaultStyle) { append(" ") }
            pos++
        }
        if (pos >= len) return
    }

    // Check for key: value pattern
    val remaining = line.substring(pos)
    val colonMatch = findKeyColon(remaining)
    if (colonMatch != null) {
        val key = remaining.substring(0, colonMatch)
        // Anchor/alias before key
        if (key.startsWith("&") || key.startsWith("*")) {
            withStyle(SpanStyle(color = YamlAnchor)) { append(key) }
        } else {
            withStyle(SpanStyle(color = YamlKey)) { append(key) }
        }
        withStyle(SpanStyle(color = YamlPunct)) { append(":") }
        val afterColon = pos + colonMatch + 1
        if (afterColon < len) {
            val rest = line.substring(afterColon)
            // Space after colon
            val valueStart = rest.indexOfFirst { it != ' ' }
            if (valueStart < 0) {
                withStyle(defaultStyle) { append(rest) }
            } else {
                withStyle(defaultStyle) { append(rest.substring(0, valueStart)) }
                highlightYamlValue(rest.substring(valueStart))
            }
        }
    } else {
        // No key — treat as a value (e.g. list item value)
        highlightYamlValue(remaining)
    }
}

/** Find the colon that separates key from value. Skips colons inside quotes. */
private fun findKeyColon(s: String): Int? {
    var i = 0
    var inSingle = false
    var inDouble = false
    while (i < s.length) {
        val c = s[i]
        when {
            c == '\'' && !inDouble -> inSingle = !inSingle
            c == '"' && !inSingle -> inDouble = !inDouble
            c == ':' && !inSingle && !inDouble && (i + 1 >= s.length || s[i + 1] == ' ') -> return i
        }
        i++
    }
    return null
}

private fun AnnotatedString.Builder.highlightYamlValue(value: String) {
    val v = value.trim()
    // Inline comment
    val commentIdx = findInlineComment(value)
    val main = if (commentIdx != null) value.substring(0, commentIdx) else value
    val comment = if (commentIdx != null) value.substring(commentIdx) else null

    val mainTrimmed = main.trim()
    when {
        // Quoted strings
        mainTrimmed.startsWith('"') || mainTrimmed.startsWith('\'') -> {
            withStyle(SpanStyle(color = YamlString)) { append(main) }
        }
        // Anchors / aliases
        mainTrimmed.startsWith('&') || mainTrimmed.startsWith('*') -> {
            withStyle(SpanStyle(color = YamlAnchor)) { append(main) }
        }
        // Booleans / null
        yamlBoolPattern.matches(mainTrimmed) -> {
            withStyle(SpanStyle(color = YamlBool)) { append(main) }
        }
        // Numbers
        yamlNumberPattern.matches(mainTrimmed) -> {
            withStyle(SpanStyle(color = YamlNumber)) { append(main) }
        }
        // Flow collections
        mainTrimmed.startsWith('[') || mainTrimmed.startsWith('{') -> {
            highlightFlowCollection(main)
        }
        // Pipe/angle (block scalar indicators)
        mainTrimmed.startsWith('|') || mainTrimmed.startsWith('>') -> {
            withStyle(SpanStyle(color = YamlPunct)) { append(main) }
        }
        // Unquoted string value
        else -> {
            withStyle(SpanStyle(color = YamlString)) { append(main) }
        }
    }
    if (comment != null) {
        withStyle(SpanStyle(color = YamlComment)) { append(comment) }
    }
}

/** Find start of inline comment (# preceded by space, outside quotes). */
private fun findInlineComment(s: String): Int? {
    var i = 0
    var inSingle = false
    var inDouble = false
    while (i < s.length) {
        val c = s[i]
        when {
            c == '\'' && !inDouble -> inSingle = !inSingle
            c == '"' && !inSingle -> inDouble = !inDouble
            c == '#' && !inSingle && !inDouble && i > 0 && s[i - 1] == ' ' -> return i
        }
        i++
    }
    return null
}

/** Highlight flow collections [a, b] / {a: b} with basic token coloring. */
private fun AnnotatedString.Builder.highlightFlowCollection(s: String) {
    for (c in s) {
        when (c) {
            '[', ']', '{', '}', ',', ':' ->
                withStyle(SpanStyle(color = YamlPunct)) { append(c.toString()) }
            else ->
                withStyle(SpanStyle(color = YamlText)) { append(c.toString()) }
        }
    }
}

// ─── Draw Left Border Modifier ───

private fun Modifier.drawLeftBorder(color: Color, borderWidth: androidx.compose.ui.unit.Dp): Modifier =
    this.drawWithContent {
        drawContent()
        drawRect(
            color = color,
            topLeft = Offset.Zero,
            size = Size(borderWidth.toPx(), size.height)
        )
    }
