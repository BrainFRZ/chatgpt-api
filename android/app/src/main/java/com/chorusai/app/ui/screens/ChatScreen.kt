package com.chorusai.app.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.snapshotFlow
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavController
import com.chorusai.app.model.ChatMessage
import com.chorusai.app.model.ModelInfo
import com.chorusai.app.ui.theme.Accent
import com.chorusai.app.ui.theme.Background
import com.chorusai.app.ui.theme.Border
import com.chorusai.app.ui.theme.Surface as SurfaceColor
import com.chorusai.app.ui.theme.SurfaceTertiary
import com.chorusai.app.ui.theme.TextMuted
import com.chorusai.app.ui.theme.TextPrimary
import com.chorusai.app.ui.theme.TextSecondary
import com.chorusai.app.ui.viewmodel.ChatNavEvent
import com.chorusai.app.ui.viewmodel.ChatViewModel
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.m3.markdownColor
import com.mikepenz.markdown.m3.markdownTypography

@Composable
fun ChatScreen(
    navController: NavController,
    viewModel: ChatViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsState()

    LaunchedEffect(Unit) {
        viewModel.navEvents.collect { event ->
            when (event) {
                is ChatNavEvent.NavigateBack -> navController.popBackStack()
            }
        }
    }

    val listState = rememberLazyListState()

    Scaffold(
        topBar = {
            ChatTopBar(
                chatName = state.chatName,
                currentModel = state.model,
                models = state.models,
                isLoadingModels = state.isLoadingModels,
                onBack = { viewModel.navigateBack() },
                onModelSelected = { viewModel.setModel(it) }
            )
        },
        containerColor = Background
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .consumeWindowInsets(padding)
                .imePadding()
        ) {
            when {
                state.isLoading -> {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .weight(1f),
                        contentAlignment = Alignment.Center
                    ) {
                        CircularProgressIndicator(color = Accent)
                    }
                }
                state.error != null -> {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .weight(1f),
                        contentAlignment = Alignment.Center
                    ) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(
                                text = state.error!!,
                                color = TextMuted,
                                style = MaterialTheme.typography.bodyLarge
                            )
                            Spacer(modifier = Modifier.height(16.dp))
                            Button(onClick = { viewModel.refresh() }) {
                                Text("Retry")
                            }
                        }
                    }
                }
                else -> {
                    val visibleMessages = state.messages.filter { it.role != "system" }
                    if (visibleMessages.isEmpty() && !state.isSending) {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .weight(1f),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                text = "No messages yet",
                                color = TextMuted,
                                style = MaterialTheme.typography.bodyLarge
                            )
                        }
                    } else {
                        MessageList(
                            messages = visibleMessages,
                            hasMoreMessages = state.hasMoreMessages,
                            isLoadingMore = state.isLoadingMore,
                            isStreaming = state.isStreaming,
                            streamingMessageId = state.streamingMessageId,
                            onLoadMore = { viewModel.loadMore() },
                            listState = listState,
                            modifier = Modifier.weight(1f)
                        )
                    }

                    // Send error banner
                    if (state.sendError != null) {
                        Surface(
                            color = MaterialTheme.colorScheme.errorContainer,
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable { viewModel.clearSendError() }
                        ) {
                            Text(
                                text = state.sendError!!,
                                color = MaterialTheme.colorScheme.onErrorContainer,
                                style = MaterialTheme.typography.bodySmall,
                                modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
                            )
                        }
                    }

                    MessageInputBar(
                        isSending = state.isSending,
                        onSend = { viewModel.sendMessage(it) }
                    )
                }
            }
        }
    }

    // Auto-scroll to the user message (top of the new pair) when sending
    val messages = state.messages.filter { it.role != "system" }
    LaunchedEffect(messages.size, state.isSending) {
        if (state.isSending && messages.size >= 2) {
            // Account for load_more header item in LazyColumn indices
            val loadMoreOffset = if (state.hasMoreMessages) 1 else 0
            listState.animateScrollToItem(messages.lastIndex - 1 + loadMoreOffset)
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ChatTopBar(
    chatName: String,
    currentModel: String?,
    models: List<ModelInfo>,
    isLoadingModels: Boolean,
    onBack: () -> Unit,
    onModelSelected: (String) -> Unit
) {
    var modelMenuExpanded by remember { mutableStateOf(false) }

    TopAppBar(
        title = {
            Column {
                Text(
                    text = chatName,
                    color = TextPrimary,
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                if (currentModel != null) {
                    val displayName = models.find { it.id == currentModel }?.name ?: currentModel
                    Text(
                        text = displayName,
                        color = TextMuted,
                        style = MaterialTheme.typography.bodySmall,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
            }
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
                    if (isLoadingModels) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(20.dp),
                            color = Accent,
                            strokeWidth = 2.dp
                        )
                    } else {
                        Icon(
                            imageVector = Icons.Filled.ArrowDropDown,
                            contentDescription = "Select model",
                            tint = TextPrimary
                        )
                    }
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
                    if (models.isEmpty() && !isLoadingModels) {
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
            containerColor = SurfaceColor
        )
    )
}

@Composable
private fun MessageList(
    messages: List<ChatMessage>,
    hasMoreMessages: Boolean,
    isLoadingMore: Boolean,
    isStreaming: Boolean,
    streamingMessageId: String?,
    onLoadMore: () -> Unit,
    listState: LazyListState,
    modifier: Modifier = Modifier
) {
    var hasScrolledToBottom by remember { mutableStateOf(false) }
    var previousMessageCount by remember { mutableStateOf(0) }
    var previousFirstMessageId by remember { mutableStateOf<String?>(null) }
    var hadLoadMoreItem by remember { mutableStateOf(false) }

    // Auto-scroll to bottom on initial load; adjust scroll after prepending older messages
    LaunchedEffect(messages.size) {
        if (!hasScrolledToBottom && messages.isNotEmpty()) {
            listState.scrollToItem(messages.lastIndex)
            hasScrolledToBottom = true
        } else if (hasScrolledToBottom && messages.isNotEmpty()) {
            val sizeChange = messages.size - previousMessageCount
            val firstIdChanged = messages.firstOrNull()?.id != previousFirstMessageId
            // Only adjust scroll for prepends (loadMore), not appends (sendMessage)
            if (sizeChange > 0 && firstIdChanged) {
                val loadMoreRemoved = hadLoadMoreItem && !hasMoreMessages
                val targetIndex = listState.firstVisibleItemIndex + sizeChange -
                        (if (loadMoreRemoved) 1 else 0)
                listState.scrollToItem(targetIndex, listState.firstVisibleItemScrollOffset)
            }
        }
        previousMessageCount = messages.size
        previousFirstMessageId = messages.firstOrNull()?.id
        hadLoadMoreItem = hasMoreMessages
    }

    // Trigger load-more when user scrolls near the top (only after initial scroll)
    LaunchedEffect(listState, hasMoreMessages) {
        snapshotFlow {
            listState.firstVisibleItemIndex to hasScrolledToBottom
        }.collect { (firstVisible, scrolled) ->
            if (scrolled && firstVisible <= 1 && hasMoreMessages) {
                onLoadMore()
            }
        }
    }

    LazyColumn(
        state = listState,
        modifier = modifier.fillMaxWidth()
    ) {
        // Load more header — always present when more messages exist (stable item count)
        if (hasMoreMessages) {
            item(key = "load_more") {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    contentAlignment = Alignment.Center
                ) {
                    if (isLoadingMore) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(24.dp),
                            color = Accent,
                            strokeWidth = 2.dp
                        )
                    }
                }
            }
        }

        itemsIndexed(
            messages,
            key = { index, msg -> msg.id ?: "msg_${index}_${msg.role}" }
        ) { _, message ->
            MessageBubble(
                message = message,
                isStreaming = isStreaming && message.id == streamingMessageId
            )
        }
    }
}

@Composable
private fun MessageInputBar(
    isSending: Boolean,
    onSend: (String) -> Unit
) {
    var text by remember { mutableStateOf("") }

    HorizontalDivider(color = Border)
    Surface(color = SurfaceColor) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp, vertical = 8.dp),
            verticalAlignment = Alignment.Bottom
        ) {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                modifier = Modifier.weight(1f),
                placeholder = { Text("Type a message…", color = TextMuted) },
                maxLines = 4,
                enabled = !isSending,
                keyboardOptions = KeyboardOptions(
                    capitalization = KeyboardCapitalization.Sentences,
                    imeAction = ImeAction.Default
                ),
                shape = RoundedCornerShape(20.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedTextColor = TextPrimary,
                    unfocusedTextColor = TextPrimary,
                    disabledTextColor = TextMuted,
                    focusedBorderColor = Accent,
                    unfocusedBorderColor = Border,
                    disabledBorderColor = Border,
                    focusedContainerColor = Background,
                    unfocusedContainerColor = Background,
                    disabledContainerColor = Background
                )
            )

            Spacer(modifier = Modifier.width(8.dp))

            IconButton(
                onClick = {
                    val msg = text.trim()
                    if (msg.isNotBlank()) {
                        onSend(msg)
                        text = ""
                    }
                },
                enabled = text.isNotBlank() && !isSending
            ) {
                if (isSending) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(24.dp),
                        color = Accent,
                        strokeWidth = 2.dp
                    )
                } else {
                    Icon(
                        imageVector = Icons.AutoMirrored.Filled.Send,
                        contentDescription = "Send",
                        tint = if (text.isNotBlank()) Accent else TextMuted
                    )
                }
            }
        }
    }
}

@Composable
private fun MessageBubble(message: ChatMessage, isStreaming: Boolean = false) {
    when (message.role) {
        "user" -> UserMessage(message)
        "assistant" -> AssistantMessage(message, isStreaming)
    }
}

@Composable
private fun UserMessage(message: ChatMessage) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(SurfaceColor)
            .padding(12.dp)
    ) {
        Text(
            text = "You",
            color = TextSecondary,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Bold
        )
        Spacer(modifier = Modifier.height(4.dp))
        ChatMarkdown(content = message.content)
    }
}

@Composable
private fun AssistantMessage(message: ChatMessage, isStreaming: Boolean = false) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(SurfaceTertiary)
            .padding(12.dp)
    ) {
        Text(
            text = "Assistant",
            color = Accent,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Bold
        )
        Spacer(modifier = Modifier.height(4.dp))

        // Collapsible reasoning section
        if (!message.reasoning.isNullOrEmpty()) {
            ReasoningSection(
                reasoning = message.reasoning,
                isStreaming = isStreaming && message.content.isEmpty()
            )
            Spacer(modifier = Modifier.height(8.dp))
        }

        // Streaming indicator when waiting for first content token
        if (isStreaming && message.content.isEmpty() && message.reasoning.isNullOrEmpty()) {
            StreamingIndicator()
        } else if (message.content.isNotEmpty()) {
            ChatMarkdown(content = message.content)
        }

        // Metadata footer (only shown after streaming completes)
        if (!isStreaming) {
            val metaParts = buildList {
                message.model?.let { add(it) }
                message.tokens?.let { add(it) }
                message.cost?.let { add(it) }
            }
            if (metaParts.isNotEmpty()) {
                Spacer(modifier = Modifier.height(8.dp))
                Row(
                    horizontalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    Text(
                        text = metaParts.joinToString(" \u00B7 "),
                        color = TextMuted,
                        fontSize = 11.sp
                    )
                }
            }
        }
    }
}

@Composable
private fun ReasoningSection(reasoning: String, isStreaming: Boolean = false) {
    var expanded by remember { mutableStateOf(false) }
    val rotationAngle by animateFloatAsState(
        targetValue = if (expanded) 90f else 0f,
        label = "reasoning_arrow"
    )

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(6.dp))
            .background(SurfaceColor)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable { expanded = !expanded }
                .padding(horizontal = 10.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = "\u25B6",
                color = TextMuted,
                fontSize = 10.sp,
                modifier = Modifier.rotate(rotationAngle)
            )
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = if (isStreaming) "Reasoning..." else "Reasoning",
                color = TextMuted,
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Medium
            )
            if (isStreaming) {
                Spacer(modifier = Modifier.width(8.dp))
                CircularProgressIndicator(
                    modifier = Modifier.size(12.dp),
                    color = TextMuted,
                    strokeWidth = 1.5.dp
                )
            }
        }

        AnimatedVisibility(visible = expanded) {
            Text(
                text = reasoning,
                color = TextMuted,
                style = MaterialTheme.typography.bodySmall.copy(
                    fontStyle = FontStyle.Italic
                ),
                modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp)
            )
        }
    }
}

@Composable
private fun StreamingIndicator() {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        CircularProgressIndicator(
            modifier = Modifier.size(16.dp),
            color = Accent,
            strokeWidth = 2.dp
        )
        Text(
            text = "Thinking...",
            color = TextMuted,
            style = MaterialTheme.typography.bodyMedium,
            fontStyle = FontStyle.Italic
        )
    }
}

@Composable
private fun ChatMarkdown(content: String) {
    val colors = markdownColor(
        text = TextPrimary,
        codeText = TextPrimary,
        inlineCodeText = TextPrimary,
        linkText = Accent,
        codeBackground = SurfaceColor,
        inlineCodeBackground = SurfaceColor,
        dividerColor = Border
    )
    val typography = markdownTypography(
        h1 = MaterialTheme.typography.headlineSmall.copy(color = TextPrimary),
        h2 = MaterialTheme.typography.titleLarge.copy(color = TextPrimary),
        h3 = MaterialTheme.typography.titleMedium.copy(color = TextPrimary),
        h4 = MaterialTheme.typography.titleSmall.copy(color = TextPrimary),
        h5 = MaterialTheme.typography.bodyLarge.copy(color = TextPrimary, fontWeight = FontWeight.Bold),
        h6 = MaterialTheme.typography.bodyMedium.copy(color = TextPrimary, fontWeight = FontWeight.Bold),
        text = MaterialTheme.typography.bodyMedium.copy(color = TextPrimary),
        code = MaterialTheme.typography.bodySmall.copy(
            fontFamily = FontFamily.Monospace,
            color = TextPrimary
        ),
        paragraph = MaterialTheme.typography.bodyMedium.copy(color = TextPrimary)
    )

    Markdown(
        content = content,
        colors = colors,
        typography = typography,
        modifier = Modifier.fillMaxWidth()
    )
}
