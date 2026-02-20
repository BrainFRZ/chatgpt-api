package com.chorusai.app.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavController
import com.chorusai.app.model.ChatSummary
import com.chorusai.app.model.ModelInfo
import com.chorusai.app.ui.navigation.Screen
import com.chorusai.app.ui.theme.Accent
import com.chorusai.app.ui.theme.Error
import com.chorusai.app.ui.theme.Surface
import com.chorusai.app.ui.theme.TextMuted
import com.chorusai.app.ui.theme.TextPrimary
import com.chorusai.app.ui.theme.TextSecondary
import com.chorusai.app.ui.viewmodel.ProjectLandingNavEvent
import com.chorusai.app.ui.viewmodel.ProjectLandingUiState
import com.chorusai.app.ui.viewmodel.ProjectLandingViewModel
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.time.format.TextStyle
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProjectLandingScreen(
    navController: NavController,
    viewModel: ProjectLandingViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()

    LaunchedEffect(Unit) {
        viewModel.navEvents.collect { event ->
            when (event) {
                is ProjectLandingNavEvent.NavigateToChat -> {
                    navController.navigate(
                        Screen.Chat.createRoute(event.chatName, event.project)
                    )
                }
                is ProjectLandingNavEvent.NavigateBack -> {
                    navController.popBackStack()
                }
            }
        }
    }

    Scaffold(
        topBar = {
            ProjectLandingTopBar(
                projectName = uiState.projectName,
                currentModel = uiState.currentModel,
                models = uiState.models,
                onBack = { viewModel.navigateBack() },
                onModelSelected = { viewModel.setProjectModel(it) }
            )
        },
        floatingActionButton = {
            FloatingActionButton(
                onClick = { viewModel.showCreateDialog() },
                containerColor = Accent
            ) {
                Icon(
                    Icons.Filled.Add,
                    contentDescription = "New chat",
                    tint = MaterialTheme.colorScheme.onPrimary
                )
            }
        },
        containerColor = MaterialTheme.colorScheme.background
    ) { padding ->
        when {
            uiState.isLoading -> {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding),
                    contentAlignment = Alignment.Center
                ) {
                    CircularProgressIndicator(color = Accent)
                }
            }
            uiState.error != null && uiState.chats.isEmpty() -> {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            text = uiState.error!!,
                            color = Error,
                            style = MaterialTheme.typography.bodyMedium
                        )
                        Spacer(modifier = Modifier.height(16.dp))
                        Button(
                            onClick = { viewModel.refresh() },
                            colors = ButtonDefaults.buttonColors(containerColor = Accent),
                            shape = RoundedCornerShape(8.dp)
                        ) {
                            Text("Retry")
                        }
                    }
                }
            }
            uiState.chats.isEmpty() -> {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(
                            Icons.AutoMirrored.Filled.Chat,
                            contentDescription = null,
                            tint = TextMuted,
                            modifier = Modifier.size(64.dp)
                        )
                        Spacer(modifier = Modifier.height(16.dp))
                        Text(
                            text = "No chats yet",
                            style = MaterialTheme.typography.titleMedium,
                            color = TextSecondary
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(
                            text = "Tap + to create one",
                            style = MaterialTheme.typography.bodyMedium,
                            color = TextMuted
                        )
                    }
                }
            }
            else -> {
                ProjectLandingContent(
                    uiState = uiState,
                    onRefresh = { viewModel.refresh() },
                    onLoadMore = { viewModel.loadMore() },
                    onChatClick = { viewModel.navigateToChat(it) },
                    onSearchQueryChange = { viewModel.updateSearchQuery(it) },
                    modifier = Modifier.padding(padding)
                )
            }
        }
    }

    if (uiState.showCreateDialog) {
        CreateChatInProjectDialog(
            error = uiState.dialogError,
            isLoading = uiState.isDialogLoading,
            onConfirm = { viewModel.createChat(it) },
            onDismiss = { viewModel.dismissCreateDialog() }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ProjectLandingTopBar(
    projectName: String,
    currentModel: String?,
    models: List<ModelInfo>,
    onBack: () -> Unit,
    onModelSelected: (String) -> Unit
) {
    var modelMenuExpanded by remember { mutableStateOf(false) }

    TopAppBar(
        title = {
            Text(
                text = projectName,
                color = MaterialTheme.colorScheme.onBackground,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
        },
        navigationIcon = {
            IconButton(onClick = onBack) {
                Icon(
                    imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                    contentDescription = "Back",
                    tint = TextPrimary
                )
            }
        },
        actions = {
            Box {
                IconButton(onClick = { modelMenuExpanded = true }) {
                    Icon(
                        imageVector = Icons.Filled.ArrowDropDown,
                        contentDescription = "Select model",
                        tint = TextPrimary
                    )
                }

                DropdownMenu(
                    expanded = modelMenuExpanded,
                    onDismissRequest = { modelMenuExpanded = false }
                ) {
                    models.forEach { model ->
                        DropdownMenuItem(
                            text = {
                                Text(
                                    text = model.name,
                                    color = if (model.id == currentModel) Accent else TextPrimary
                                )
                            },
                            onClick = {
                                onModelSelected(model.id)
                                modelMenuExpanded = false
                            }
                        )
                    }
                    if (models.isEmpty()) {
                        DropdownMenuItem(
                            text = {
                                Text(
                                    text = "No models available",
                                    color = TextMuted
                                )
                            },
                            onClick = { modelMenuExpanded = false }
                        )
                    }
                }
            }
        },
        colors = TopAppBarDefaults.topAppBarColors(
            containerColor = MaterialTheme.colorScheme.background
        )
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ProjectLandingContent(
    uiState: ProjectLandingUiState,
    onRefresh: () -> Unit,
    onLoadMore: () -> Unit,
    onChatClick: (String) -> Unit,
    onSearchQueryChange: (String) -> Unit,
    modifier: Modifier = Modifier
) {
    val query = uiState.searchQuery.trim().lowercase()
    val filteredChats = remember(uiState.chats, query) {
        if (query.isEmpty()) {
            uiState.chats
        } else {
            uiState.chats.filter { chat ->
                chat.name.lowercase().contains(query) ||
                    chat.lastMessage.lowercase().contains(query)
            }
        }
    }
    val grouped = remember(filteredChats) {
        groupChatsByDate(filteredChats)
    }

    PullToRefreshBox(
        isRefreshing = uiState.isRefreshing,
        onRefresh = onRefresh,
        modifier = modifier.fillMaxSize()
    ) {
        LazyColumn(modifier = Modifier.fillMaxSize()) {
            item(key = "search") {
                OutlinedTextField(
                    value = uiState.searchQuery,
                    onValueChange = onSearchQueryChange,
                    placeholder = { Text("Search chats...", color = TextMuted) },
                    leadingIcon = {
                        Icon(Icons.Filled.Search, contentDescription = null, tint = TextMuted)
                    },
                    singleLine = true,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = Accent,
                        unfocusedBorderColor = TextSecondary,
                        cursorColor = Accent,
                        focusedLeadingIconColor = Accent
                    )
                )
            }

            if (filteredChats.isEmpty() && query.isNotEmpty()) {
                item(key = "no_results") {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(32.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            text = "No chats matching \"${uiState.searchQuery.trim()}\"",
                            color = TextMuted,
                            style = MaterialTheme.typography.bodyMedium
                        )
                    }
                }
            }

            grouped.forEach { (label, chats) ->
                item(key = "header_$label") {
                    DateGroupHeader(label = label)
                }
                items(chats, key = { "${label}_chat_${it.name}" }) { chat ->
                    ChatCard(
                        chat = chat,
                        onClick = { onChatClick(chat.name) }
                    )
                }
            }

            if (uiState.hasMore && query.isEmpty()) {
                item(key = "load_more") {
                    LaunchedEffect(uiState.chats.size) { onLoadMore() }
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(16.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        CircularProgressIndicator(
                            color = Accent,
                            modifier = Modifier.size(24.dp),
                            strokeWidth = 2.dp
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun DateGroupHeader(label: String) {
    Text(
        text = label,
        style = MaterialTheme.typography.titleSmall,
        color = TextSecondary,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)
    )
}

@Composable
private fun ChatCard(
    chat: ChatSummary,
    onClick: () -> Unit
) {
    val timeText = formatTime(chat.lastActive)

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.Top
    ) {
        Icon(
            Icons.AutoMirrored.Filled.Chat,
            contentDescription = null,
            tint = TextSecondary,
            modifier = Modifier
                .size(24.dp)
                .padding(top = 2.dp)
        )
        Spacer(modifier = Modifier.width(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = chat.name,
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onBackground,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f, fill = false)
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = timeText,
                    style = MaterialTheme.typography.bodySmall,
                    color = TextMuted
                )
            }
            if (chat.lastMessage.isNotBlank()) {
                Spacer(modifier = Modifier.height(2.dp))
                Text(
                    text = chat.lastMessage,
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextMuted,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
            Spacer(modifier = Modifier.height(2.dp))
            Text(
                text = "${chat.messageCount} messages",
                style = MaterialTheme.typography.bodySmall,
                color = TextMuted
            )
        }
    }
}

@Composable
private fun CreateChatInProjectDialog(
    error: String?,
    isLoading: Boolean,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit
) {
    var chatName by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = { if (!isLoading) onDismiss() },
        containerColor = Surface,
        title = { Text("New Chat", color = MaterialTheme.colorScheme.onBackground) },
        text = {
            Column {
                OutlinedTextField(
                    value = chatName,
                    onValueChange = { chatName = it },
                    label = { Text("Chat name") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = Accent,
                        unfocusedBorderColor = TextSecondary,
                        cursorColor = Accent,
                        focusedLabelColor = Accent
                    )
                )
                if (error != null) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(text = error, color = Error, style = MaterialTheme.typography.bodySmall)
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onConfirm(chatName) },
                enabled = chatName.isNotBlank() && !isLoading,
                colors = ButtonDefaults.buttonColors(containerColor = Accent),
                shape = RoundedCornerShape(8.dp)
            ) {
                if (isLoading) {
                    CircularProgressIndicator(
                        color = MaterialTheme.colorScheme.onPrimary,
                        strokeWidth = 2.dp,
                        modifier = Modifier.size(16.dp)
                    )
                } else {
                    Text("Create")
                }
            }
        },
        dismissButton = {
            TextButton(
                onClick = onDismiss,
                enabled = !isLoading
            ) {
                Text("Cancel", color = TextSecondary)
            }
        }
    )
}

// --- Date grouping helpers ---

private val deviceZone: ZoneId = ZoneId.systemDefault()

private fun parseIsoDateTime(iso: String): LocalDateTime? {
    // Try offset-aware formats first (e.g. "2026-02-20T14:30:00Z" or "+00:00")
    try {
        return OffsetDateTime.parse(iso).atZoneSameInstant(deviceZone).toLocalDateTime()
    } catch (_: DateTimeParseException) {}
    try {
        return ZonedDateTime.parse(iso).withZoneSameInstant(deviceZone).toLocalDateTime()
    } catch (_: DateTimeParseException) {}
    // Fallback: offset-naive with variable fractional seconds (assumed device-local)
    try {
        return LocalDateTime.parse(iso, DateTimeFormatter.ISO_LOCAL_DATE_TIME)
    } catch (_: DateTimeParseException) {}
    return null
}

private fun groupChatsByDate(chats: List<ChatSummary>): List<Pair<String, List<ChatSummary>>> {
    val today = LocalDate.now()
    val yesterday = today.minusDays(1)
    val weekAgo = today.minusDays(7)

    val groups = linkedMapOf<String, MutableList<ChatSummary>>()
    for (chat in chats) {
        val date = parseIsoDateTime(chat.lastActive)?.toLocalDate()
        val label = when {
            date == null -> "Older"
            date == today -> "Today"
            date == yesterday -> "Yesterday"
            date.isAfter(weekAgo) -> date.dayOfWeek.getDisplayName(TextStyle.FULL, Locale.getDefault())
            else -> date.format(DateTimeFormatter.ofPattern("MMMM yyyy"))
        }
        groups.getOrPut(label) { mutableListOf() }.add(chat)
    }
    return groups.map { (label, list) -> label to list.toList() }
}

private val timeOnlyFormatter = DateTimeFormatter.ofPattern("h:mm a")
private val dateTimeFormatter = DateTimeFormatter.ofPattern("MMM d, h:mm a")

private fun formatTime(iso: String): String {
    val dateTime = parseIsoDateTime(iso) ?: return ""
    val today = LocalDate.now()
    val date = dateTime.toLocalDate()
    return if (date == today || date == today.minusDays(1)) {
        dateTime.format(timeOnlyFormatter)
    } else {
        dateTime.format(dateTimeFormatter)
    }
}
