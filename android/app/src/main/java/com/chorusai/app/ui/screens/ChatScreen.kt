package com.chorusai.app.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.snapshotFlow
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.People
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.BookmarkBorder
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowLeft
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.activity.compose.BackHandler
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
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
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
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
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.unit.Dp
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
import com.chorusai.app.ui.theme.Error
import com.chorusai.app.ui.theme.Success
import com.chorusai.app.ui.theme.Surface as SurfaceColor
import com.chorusai.app.ui.theme.SurfaceTertiary
import com.chorusai.app.ui.theme.TextMuted
import com.chorusai.app.ui.theme.TextPrimary
import com.chorusai.app.ui.theme.TextSecondary
import com.chorusai.app.ui.navigation.Screen
import com.chorusai.app.ui.viewmodel.ChatNavEvent
import com.chorusai.app.ui.viewmodel.ChatViewModel
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.m3.markdownColor
import com.mikepenz.markdown.m3.markdownTypography

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    navController: NavController,
    viewModel: ChatViewModel = hiltViewModel()
) {
    val state by viewModel.uiState.collectAsState()
    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(Unit) {
        viewModel.navEvents.collect { event ->
            when (event) {
                is ChatNavEvent.NavigateBack -> navController.popBackStack()
                is ChatNavEvent.NavigateToLogin -> {
                    navController.navigate(Screen.Login.route) {
                        popUpTo(0) { inclusive = true }
                    }
                }
            }
        }
    }

    // Show send errors as snackbar
    LaunchedEffect(state.sendError) {
        state.sendError?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearSendError()
        }
    }

    // BackHandler: cancel edit → dismiss bookmark → navigate back (clearing lastChat)
    BackHandler(enabled = true) {
        when {
            state.editingMessageId != null -> viewModel.cancelEditMessage()
            state.bookmarkEditingMessageId != null -> viewModel.dismissBookmark()
            state.bookmarkPopupMessageId != null -> viewModel.dismissBookmark()
            else -> viewModel.navigateBack()
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
                viewerCount = state.viewerCount,
                anthropicSync = state.anthropicSync,
                onBack = { viewModel.navigateBack() },
                onModelSelected = { viewModel.setModel(it) },
                onToggleSync = { viewModel.setAnthropicSync(it) }
            )
        },
        snackbarHost = { SnackbarHost(snackbarHostState) },
        containerColor = Background
    ) { padding ->
        val ps = state.pipelineState
        val hackState = state.hackState
        val hasCharacterPanel = hackState?.active == true ||
            (ps != null && (ps.sceneState.pcsPresent.isNotEmpty() ||
             ps.sceneState.npcsPresent.isNotEmpty() ||
             ps.characterStates.isNotEmpty()))

        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .consumeWindowInsets(padding)
                .imePadding()
        ) {
            Column(modifier = Modifier.fillMaxSize()) {
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
                            PullToRefreshBox(
                                isRefreshing = state.isRefreshing,
                                onRefresh = { viewModel.refresh() },
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .weight(1f)
                            ) {
                                Box(
                                    modifier = Modifier
                                        .fillMaxSize()
                                        .verticalScroll(rememberScrollState()),
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
                                            text = "No messages yet",
                                            style = MaterialTheme.typography.titleMedium,
                                            color = TextSecondary
                                        )
                                        Spacer(modifier = Modifier.height(8.dp))
                                        Text(
                                            text = "Send a message to start",
                                            style = MaterialTheme.typography.bodyMedium,
                                            color = TextMuted
                                        )
                                    }
                                }
                            }
                        } else {
                            PullToRefreshBox(
                                isRefreshing = state.isRefreshing,
                                onRefresh = { viewModel.refresh() },
                                modifier = Modifier.weight(1f)
                            ) {
                                MessageList(
                                    messages = visibleMessages,
                                    totalMessages = state.totalMessages,
                                    contextStartIndex = state.contextStartIndex,
                                    hasMoreMessages = state.hasMoreMessages,
                                    isLoadingMore = state.isLoadingMore,
                                    isStreaming = state.isStreaming || state.isRemoteStreaming,
                                    streamingMessageId = state.streamingMessageId,
                                    editingMessageId = state.editingMessageId,
                                    editingMessageContent = state.editingMessageContent,
                                    isSending = state.isSending || state.isRemoteStreaming,
                                    scrollToBottomTrigger = state.scrollToBottomTrigger,
                                    bookmarkEditingMessageId = state.bookmarkEditingMessageId,
                                    bookmarkEditingText = state.bookmarkEditingText,
                                    bookmarkPopupMessageId = state.bookmarkPopupMessageId,
                                    onLoadMore = { viewModel.loadMore() },
                                    onGetSiblings = { viewModel.getSiblings(it) },
                                    onSwitchBranch = { viewModel.switchBranch(it) },
                                    onStartEdit = { viewModel.startEditMessage(it) },
                                    onUpdateEditContent = { viewModel.updateEditContent(it) },
                                    onSaveEdit = { viewModel.saveEditMessage() },
                                    onCancelEdit = { viewModel.cancelEditMessage() },
                                    onToggleBookmarkPopup = { viewModel.toggleBookmarkPopup(it) },
                                    onStartBookmarkEdit = { viewModel.startBookmarkEdit(it) },
                                    onUpdateBookmarkText = { viewModel.updateBookmarkText(it) },
                                    onSaveBookmark = { viewModel.saveBookmark() },
                                    onDismissBookmark = { viewModel.dismissBookmark() },
                                    listState = listState,
                                    modifier = Modifier.fillMaxSize()
                                )
                            }
                        }

                        MessageInputBar(
                            isSending = state.isSending || state.isRemoteStreaming,
                            onSend = { viewModel.sendMessage(it) },
                            bottomPadding = if (hasCharacterPanel) 34.dp else 0.dp
                        )

                    }
                }
            }

            // Character panel overlay
            if (hasCharacterPanel) {
                CharacterPanel(
                    pipelineState = ps ?: com.chorusai.app.model.PipelineState(),
                    gameSystem = state.gameSystem,
                    hackState = hackState,
                    characterSheetFiles = state.characterSheetFiles,
                    onFetchCharacterSheet = { viewModel.fetchCharacterSheet() },
                    modifier = Modifier.align(Alignment.BottomCenter)
                )
            }
        }
    }

    // Auto-scroll to the user message (top of the new pair) when sending locally
    val messages = state.messages.filter { it.role != "system" }
    LaunchedEffect(messages.size, state.isSending) {
        if (state.isSending && messages.size >= 2) {
            // Account for load_more header item in LazyColumn indices
            val loadMoreOffset = if (state.hasMoreMessages) 1 else 0
            listState.animateScrollToItem(messages.lastIndex - 1 + loadMoreOffset)
        }
    }

    if (state.chatDeleted) {
        AlertDialog(
            onDismissRequest = { viewModel.navigateBack() },
            title = { Text("Chat deleted") },
            text = {
                Text(
                    "\"${state.chatName}\" was deleted from another session.",
                    color = TextPrimary
                )
            },
            confirmButton = {
                Button(onClick = { viewModel.navigateBack() }) {
                    Text("OK")
                }
            }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ChatTopBar(
    chatName: String,
    currentModel: String?,
    models: List<ModelInfo>,
    isLoadingModels: Boolean,
    viewerCount: Int,
    anthropicSync: Boolean,
    onBack: () -> Unit,
    onModelSelected: (String) -> Unit,
    onToggleSync: (Boolean) -> Unit
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
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    if (currentModel != null) {
                        val displayName = models.find { it.id == currentModel }?.name ?: currentModel
                        Text(
                            text = displayName,
                            color = TextMuted,
                            style = MaterialTheme.typography.bodySmall,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.weight(1f, fill = false)
                        )
                    }
                    if (viewerCount > 1) {
                        Icon(
                            imageVector = Icons.Filled.People,
                            contentDescription = "Viewers",
                            tint = TextMuted,
                            modifier = Modifier.size(14.dp)
                        )
                        Text(
                            text = viewerCount.toString(),
                            color = TextMuted,
                            style = MaterialTheme.typography.bodySmall
                        )
                    }
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
            if (currentModel?.startsWith("claude") == true) {
                SyncToggle(
                    isSync = anthropicSync,
                    onToggle = { onToggleSync(!anthropicSync) }
                )
            }
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
private fun SyncToggle(
    isSync: Boolean,
    onToggle: () -> Unit
) {
    val thumbOffset by animateDpAsState(
        targetValue = if (isSync) 20.dp else 2.dp,
        label = "sync_thumb"
    )
    val trackColor = if (isSync) Color(0xFF4ADE80) else Color(0xFF4A4A6E)

    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
        modifier = Modifier.padding(end = 4.dp)
    ) {
        Text(
            text = "Async",
            color = TextMuted,
            fontSize = 11.sp
        )
        Box(
            modifier = Modifier
                .width(36.dp)
                .height(18.dp)
                .clip(RoundedCornerShape(9.dp))
                .background(trackColor)
                .clickable(onClick = onToggle),
            contentAlignment = Alignment.CenterStart
        ) {
            Box(
                modifier = Modifier
                    .padding(start = thumbOffset)
                    .size(14.dp)
                    .clip(CircleShape)
                    .background(Color.White)
            )
        }
        Text(
            text = "Sync",
            color = TextMuted,
            fontSize = 11.sp
        )
    }
}

@Composable
private fun MessageList(
    messages: List<ChatMessage>,
    totalMessages: Int,
    contextStartIndex: Int,
    hasMoreMessages: Boolean,
    isLoadingMore: Boolean,
    isStreaming: Boolean,
    streamingMessageId: String?,
    editingMessageId: String?,
    editingMessageContent: String,
    isSending: Boolean,
    scrollToBottomTrigger: Int,
    bookmarkEditingMessageId: String?,
    bookmarkEditingText: String,
    bookmarkPopupMessageId: String?,
    onLoadMore: () -> Unit,
    onGetSiblings: (String) -> List<ChatMessage>,
    onSwitchBranch: (String) -> Unit,
    onStartEdit: (String) -> Unit,
    onUpdateEditContent: (String) -> Unit,
    onSaveEdit: () -> Unit,
    onCancelEdit: () -> Unit,
    onToggleBookmarkPopup: (String) -> Unit,
    onStartBookmarkEdit: (String) -> Unit,
    onUpdateBookmarkText: (String) -> Unit,
    onSaveBookmark: () -> Unit,
    onDismissBookmark: () -> Unit,
    listState: LazyListState,
    modifier: Modifier = Modifier
) {
    var hasScrolledToBottom by remember { mutableStateOf(false) }
    var previousMessageCount by remember { mutableStateOf(0) }
    var previousFirstMessageId by remember { mutableStateOf<String?>(null) }
    var hadLoadMoreItem by remember { mutableStateOf(false) }

    // Re-scroll to bottom after branch switch (full list replacement)
    LaunchedEffect(scrollToBottomTrigger) {
        if (scrollToBottomTrigger > 0 && messages.isNotEmpty()) {
            val loadMoreOffset = if (hasMoreMessages) 1 else 0
            listState.scrollToItem(messages.lastIndex + loadMoreOffset)
        }
    }

    // Auto-scroll to bottom on initial load; adjust scroll after prepending older messages
    LaunchedEffect(messages.size) {
        if (!hasScrolledToBottom && messages.isNotEmpty()) {
            val loadMoreOffset = if (hasMoreMessages) 1 else 0
            listState.scrollToItem(messages.lastIndex + loadMoreOffset)
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
        modifier = modifier
            .fillMaxWidth()
            .fadingScrollbar(listState)
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
        ) { index, message ->
            // Context graying: calculate backend index and compare to contextStartIndex
            val firstDisplayedBackendIndex = totalMessages - messages.size
            val actualBackendIndex = firstDisplayedBackendIndex + index
            val isInContext = actualBackendIndex >= contextStartIndex

            // Branch info for user messages
            val siblings = if (message.role == "user" && message.id != null) {
                onGetSiblings(message.id!!)
            } else {
                emptyList()
            }

            val isEditing = editingMessageId != null && editingMessageId == message.id

            MessageBubble(
                message = message,
                isStreaming = isStreaming && message.id == streamingMessageId,
                isInContext = isInContext,
                siblings = siblings,
                isEditing = isEditing,
                editContent = if (isEditing) editingMessageContent else "",
                isSending = isSending,
                isBookmarkEditing = bookmarkEditingMessageId == message.id,
                bookmarkEditingText = if (bookmarkEditingMessageId == message.id) bookmarkEditingText else "",
                isBookmarkPopupShowing = bookmarkPopupMessageId == message.id,
                onSwitchBranch = onSwitchBranch,
                onStartEdit = { message.id?.let { onStartEdit(it) } },
                onUpdateEditContent = onUpdateEditContent,
                onSaveEdit = onSaveEdit,
                onCancelEdit = onCancelEdit,
                onToggleBookmarkPopup = { message.id?.let { onToggleBookmarkPopup(it) } },
                onStartBookmarkEdit = { message.id?.let { onStartBookmarkEdit(it) } },
                onUpdateBookmarkText = onUpdateBookmarkText,
                onSaveBookmark = onSaveBookmark,
                onDismissBookmark = onDismissBookmark
            )
        }
    }
}

@Composable
private fun MessageInputBar(
    isSending: Boolean,
    onSend: (String) -> Unit,
    bottomPadding: Dp = 0.dp
) {
    var text by remember { mutableStateOf("") }

    HorizontalDivider(color = Border)
    Surface(color = SurfaceColor) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(start = 8.dp, end = 8.dp, top = 8.dp, bottom = 8.dp + bottomPadding),
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
private fun MessageBubble(
    message: ChatMessage,
    isStreaming: Boolean = false,
    isInContext: Boolean = true,
    siblings: List<ChatMessage> = emptyList(),
    isEditing: Boolean = false,
    editContent: String = "",
    isSending: Boolean = false,
    isBookmarkEditing: Boolean = false,
    bookmarkEditingText: String = "",
    isBookmarkPopupShowing: Boolean = false,
    onSwitchBranch: (String) -> Unit = {},
    onStartEdit: () -> Unit = {},
    onUpdateEditContent: (String) -> Unit = {},
    onSaveEdit: () -> Unit = {},
    onCancelEdit: () -> Unit = {},
    onToggleBookmarkPopup: () -> Unit = {},
    onStartBookmarkEdit: () -> Unit = {},
    onUpdateBookmarkText: (String) -> Unit = {},
    onSaveBookmark: () -> Unit = {},
    onDismissBookmark: () -> Unit = {}
) {
    when (message.role) {
        "user" -> UserMessage(
            message = message,
            isInContext = isInContext,
            siblings = siblings,
            isEditing = isEditing,
            editContent = editContent,
            isSending = isSending,
            isBookmarkEditing = isBookmarkEditing,
            bookmarkEditingText = bookmarkEditingText,
            isBookmarkPopupShowing = isBookmarkPopupShowing,
            onSwitchBranch = onSwitchBranch,
            onStartEdit = onStartEdit,
            onUpdateEditContent = onUpdateEditContent,
            onSaveEdit = onSaveEdit,
            onCancelEdit = onCancelEdit,
            onToggleBookmarkPopup = onToggleBookmarkPopup,
            onStartBookmarkEdit = onStartBookmarkEdit,
            onUpdateBookmarkText = onUpdateBookmarkText,
            onSaveBookmark = onSaveBookmark,
            onDismissBookmark = onDismissBookmark
        )
        "assistant" -> AssistantMessage(message, isStreaming, isInContext)
    }
}

@Composable
private fun UserMessage(
    message: ChatMessage,
    isInContext: Boolean = true,
    siblings: List<ChatMessage> = emptyList(),
    isEditing: Boolean = false,
    editContent: String = "",
    isSending: Boolean = false,
    isBookmarkEditing: Boolean = false,
    bookmarkEditingText: String = "",
    isBookmarkPopupShowing: Boolean = false,
    onSwitchBranch: (String) -> Unit = {},
    onStartEdit: () -> Unit = {},
    onUpdateEditContent: (String) -> Unit = {},
    onSaveEdit: () -> Unit = {},
    onCancelEdit: () -> Unit = {},
    onToggleBookmarkPopup: () -> Unit = {},
    onStartBookmarkEdit: () -> Unit = {},
    onUpdateBookmarkText: (String) -> Unit = {},
    onSaveBookmark: () -> Unit = {},
    onDismissBookmark: () -> Unit = {}
) {
    val bgColor = if (isInContext) SurfaceColor else SurfaceColor.copy(alpha = 0.5f)
    val contentAlpha = if (isInContext) 1f else 0.5f

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(bgColor)
            .padding(12.dp)
    ) {
        // Header row: "You" label + branch nav + edit button
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text(
                text = "You",
                color = TextSecondary.copy(alpha = contentAlpha),
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Bold
            )

            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(2.dp)
            ) {
                // Branch navigation: ◀ N/M ▶
                if (siblings.size > 1 && message.id != null) {
                    val currentIndex = siblings.indexOfFirst { it.id == message.id }
                    if (currentIndex >= 0) {
                        val hasPrev = currentIndex > 0
                        val hasNext = currentIndex < siblings.size - 1

                        IconButton(
                            onClick = {
                                val prevId = siblings.getOrNull(currentIndex - 1)?.id
                                if (hasPrev && prevId != null) onSwitchBranch(prevId)
                            },
                            enabled = hasPrev,
                            modifier = Modifier.size(24.dp)
                        ) {
                            Icon(
                                imageVector = Icons.AutoMirrored.Filled.KeyboardArrowLeft,
                                contentDescription = "Previous branch",
                                tint = if (hasPrev) TextMuted else TextMuted.copy(alpha = 0.3f),
                                modifier = Modifier.size(16.dp)
                            )
                        }

                        Text(
                            text = "${currentIndex + 1}/${siblings.size}",
                            color = TextMuted,
                            fontSize = 11.sp,
                            modifier = Modifier.padding(horizontal = 2.dp)
                        )

                        IconButton(
                            onClick = {
                                val nextId = siblings.getOrNull(currentIndex + 1)?.id
                                if (hasNext && nextId != null) onSwitchBranch(nextId)
                            },
                            enabled = hasNext,
                            modifier = Modifier.size(24.dp)
                        ) {
                            Icon(
                                imageVector = Icons.AutoMirrored.Filled.KeyboardArrowRight,
                                contentDescription = "Next branch",
                                tint = if (hasNext) TextMuted else TextMuted.copy(alpha = 0.3f),
                                modifier = Modifier.size(16.dp)
                            )
                        }
                    }
                }

                // Edit button (hidden while sending or editing another message)
                if (!isEditing && !isSending) {
                    IconButton(
                        onClick = onStartEdit,
                        modifier = Modifier.size(24.dp)
                    ) {
                        Icon(
                            imageVector = Icons.Filled.Edit,
                            contentDescription = "Edit message",
                            tint = TextMuted,
                            modifier = Modifier.size(14.dp)
                        )
                    }
                }

                // Bookmark icon (hidden while sending or editing)
                if (!isSending && !isEditing) {
                    val hasBookmark = !message.bookmark.isNullOrEmpty()
                    IconButton(
                        onClick = onToggleBookmarkPopup,
                        modifier = Modifier.size(24.dp)
                    ) {
                        Icon(
                            imageVector = if (hasBookmark) Icons.Filled.Bookmark else Icons.Filled.BookmarkBorder,
                            contentDescription = if (hasBookmark) "View bookmark" else "Add bookmark",
                            tint = if (hasBookmark) Accent else TextMuted,
                            modifier = Modifier.size(14.dp)
                        )
                    }
                }
            }
        }

        Spacer(modifier = Modifier.height(4.dp))

        if (isEditing) {
            // Inline edit mode
            val focusRequester = remember { FocusRequester() }
            LaunchedEffect(Unit) { focusRequester.requestFocus() }
            OutlinedTextField(
                value = editContent,
                onValueChange = onUpdateEditContent,
                modifier = Modifier.fillMaxWidth().focusRequester(focusRequester),
                maxLines = 8,
                shape = RoundedCornerShape(8.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedTextColor = TextPrimary,
                    unfocusedTextColor = TextPrimary,
                    focusedBorderColor = Accent,
                    unfocusedBorderColor = Border,
                    focusedContainerColor = Background,
                    unfocusedContainerColor = Background
                )
            )
            Spacer(modifier = Modifier.height(8.dp))
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                IconButton(
                    onClick = onSaveEdit,
                    enabled = editContent.isNotBlank(),
                    modifier = Modifier.size(32.dp)
                ) {
                    Icon(
                        imageVector = Icons.Filled.Check,
                        contentDescription = "Save edit",
                        tint = if (editContent.isNotBlank()) Success else TextMuted,
                        modifier = Modifier.size(20.dp)
                    )
                }
                IconButton(
                    onClick = onCancelEdit,
                    modifier = Modifier.size(32.dp)
                ) {
                    Icon(
                        imageVector = Icons.Filled.Close,
                        contentDescription = "Cancel edit",
                        tint = Error,
                        modifier = Modifier.size(20.dp)
                    )
                }
            }
        } else {
            ChatMarkdown(content = message.content)
        }

        // Bookmark popup
        if (isBookmarkPopupShowing) {
            BookmarkPopup(
                text = message.bookmark ?: "",
                onTapText = onStartBookmarkEdit
            )
        }

        // Bookmark editor
        if (isBookmarkEditing) {
            BookmarkEditor(
                text = bookmarkEditingText,
                onTextChange = onUpdateBookmarkText,
                onSave = onSaveBookmark,
                onDismiss = onDismissBookmark
            )
        }
    }
}

@Composable
private fun BookmarkPopup(
    text: String,
    onTapText: () -> Unit
) {
    Spacer(modifier = Modifier.height(8.dp))
    Card(
        colors = CardDefaults.cardColors(containerColor = Background),
        border = BorderStroke(1.dp, Accent.copy(alpha = 0.3f)),
        shape = RoundedCornerShape(6.dp)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable(onClick = onTapText)
                .padding(8.dp),
            verticalAlignment = Alignment.Top,
            horizontalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            Icon(
                imageVector = Icons.Filled.Bookmark,
                contentDescription = null,
                tint = Accent,
                modifier = Modifier.size(14.dp)
            )
            Text(
                text = text,
                color = TextSecondary,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis
            )
        }
    }
}

@Composable
private fun BookmarkEditor(
    text: String,
    onTextChange: (String) -> Unit,
    onSave: () -> Unit,
    onDismiss: () -> Unit
) {
    Spacer(modifier = Modifier.height(8.dp))
    val focusRequester = remember { FocusRequester() }
    var hasFocus by remember { mutableStateOf(false) }
    var committed by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { focusRequester.requestFocus() }

    OutlinedTextField(
        value = text,
        onValueChange = onTextChange,
        modifier = Modifier
            .fillMaxWidth()
            .focusRequester(focusRequester)
            .onFocusChanged { focusState ->
                if (hasFocus && !focusState.isFocused && !committed) {
                    committed = true
                    // Focus lost → save if non-empty, dismiss if empty
                    if (text.trim().isNotEmpty()) onSave() else onDismiss()
                }
                hasFocus = focusState.isFocused
            },
        placeholder = { Text("Bookmark this message…", color = TextMuted) },
        maxLines = 3,
        shape = RoundedCornerShape(6.dp),
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
        keyboardActions = KeyboardActions(onDone = {
            if (!committed) {
                committed = true
                onSave()
            }
        }),
        colors = OutlinedTextFieldDefaults.colors(
            focusedTextColor = TextPrimary,
            unfocusedTextColor = TextPrimary,
            focusedBorderColor = Accent,
            unfocusedBorderColor = Border,
            focusedContainerColor = Background,
            unfocusedContainerColor = Background
        )
    )
}

@Composable
private fun AssistantMessage(
    message: ChatMessage,
    isStreaming: Boolean = false,
    isInContext: Boolean = true
) {
    val bgColor = if (isInContext) SurfaceTertiary else SurfaceTertiary.copy(alpha = 0.5f)
    val contentAlpha = if (isInContext) 1f else 0.5f

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(bgColor)
            .padding(12.dp)
    ) {
        Text(
            text = "Assistant",
            color = Accent.copy(alpha = contentAlpha),
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
private fun Modifier.fadingScrollbar(
    listState: LazyListState,
    width: Dp = 4.dp,
    minThumbHeight: Dp = 32.dp
): Modifier {
    val alpha = remember { Animatable(0f) }

    // Fade in when scrolling, fade out after idle
    LaunchedEffect(listState) {
        snapshotFlow { listState.isScrollInProgress }
            .collect { scrolling ->
                if (scrolling) {
                    alpha.snapTo(1f)
                } else {
                    alpha.animateTo(0f, animationSpec = tween(durationMillis = 800, delayMillis = 600))
                }
            }
    }

    val currentAlpha = alpha.value
    return this.then(
        if (currentAlpha > 0f) {
            Modifier.drawWithContent {
                drawContent()

                val totalItems = listState.layoutInfo.totalItemsCount
                val visibleItems = listState.layoutInfo.visibleItemsInfo
                if (totalItems == 0 || visibleItems.isEmpty()) return@drawWithContent

                val viewportHeight = listState.layoutInfo.viewportSize.height.toFloat()
                val totalHeight = listState.layoutInfo.visibleItemsInfo.let { items ->
                    val avgItemHeight = items.sumOf { it.size } / items.size.toFloat()
                    avgItemHeight * totalItems
                }
                if (totalHeight <= viewportHeight) return@drawWithContent

                val thumbHeight = (viewportHeight / totalHeight * viewportHeight)
                    .coerceAtLeast(minThumbHeight.toPx())

                val firstVisibleItem = visibleItems.first()
                val scrollOffset = firstVisibleItem.index * (totalHeight / totalItems) - firstVisibleItem.offset
                val scrollFraction = scrollOffset / (totalHeight - viewportHeight)
                val thumbOffset = scrollFraction * (viewportHeight - thumbHeight)

                drawRoundRect(
                    color = TextMuted,
                    topLeft = Offset(size.width - width.toPx() - 2.dp.toPx(), thumbOffset.coerceAtLeast(0f)),
                    size = Size(width.toPx(), thumbHeight),
                    cornerRadius = CornerRadius(width.toPx() / 2f),
                    alpha = currentAlpha * 0.5f
                )
            }
        } else {
            Modifier
        }
    )
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
