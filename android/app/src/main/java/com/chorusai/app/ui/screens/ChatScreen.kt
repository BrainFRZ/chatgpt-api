package com.chorusai.app.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.snapshotFlow
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavController
import com.chorusai.app.model.ChatMessage
import com.chorusai.app.ui.theme.Accent
import com.chorusai.app.ui.theme.Background
import com.chorusai.app.ui.theme.Border
import com.chorusai.app.ui.theme.Surface
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

    Scaffold(
        topBar = {
            ChatTopBar(
                chatName = state.chatName,
                onBack = { viewModel.navigateBack() }
            )
        },
        containerColor = Background
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            when {
                state.isLoading -> {
                    CircularProgressIndicator(
                        modifier = Modifier.align(Alignment.Center),
                        color = Accent
                    )
                }
                state.error != null -> {
                    Column(
                        modifier = Modifier.align(Alignment.Center),
                        horizontalAlignment = Alignment.CenterHorizontally
                    ) {
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
                state.messages.isEmpty() -> {
                    Text(
                        text = "No messages yet",
                        color = TextMuted,
                        style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.align(Alignment.Center)
                    )
                }
                else -> {
                    MessageList(
                        messages = state.messages,
                        hasMoreMessages = state.hasMoreMessages,
                        isLoadingMore = state.isLoadingMore,
                        onLoadMore = { viewModel.loadMore() }
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ChatTopBar(chatName: String, onBack: () -> Unit) {
    TopAppBar(
        title = {
            Text(
                text = chatName,
                color = TextPrimary,
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
        colors = TopAppBarDefaults.topAppBarColors(
            containerColor = Surface
        )
    )
}

@Composable
private fun MessageList(
    messages: List<ChatMessage>,
    hasMoreMessages: Boolean,
    isLoadingMore: Boolean,
    onLoadMore: () -> Unit
) {
    val listState = rememberLazyListState()
    var hasScrolledToBottom by remember { mutableStateOf(false) }
    var previousMessageCount by remember { mutableStateOf(0) }
    var hadLoadMoreItem by remember { mutableStateOf(false) }

    // Auto-scroll to bottom on initial load; adjust scroll after prepending older messages
    LaunchedEffect(messages.size) {
        if (!hasScrolledToBottom && messages.isNotEmpty()) {
            listState.scrollToItem(messages.lastIndex)
            hasScrolledToBottom = true
        } else if (hasScrolledToBottom) {
            val prependedCount = messages.size - previousMessageCount
            if (prependedCount > 0) {
                // If the load_more item was removed (last page), offset by -1
                val loadMoreRemoved = hadLoadMoreItem && !hasMoreMessages
                val targetIndex = listState.firstVisibleItemIndex + prependedCount -
                        (if (loadMoreRemoved) 1 else 0)
                listState.scrollToItem(targetIndex, listState.firstVisibleItemScrollOffset)
            }
        }
        previousMessageCount = messages.size
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
        modifier = Modifier.fillMaxSize()
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
            MessageBubble(message = message)
        }
    }
}

@Composable
private fun MessageBubble(message: ChatMessage) {
    when (message.role) {
        "system" -> SystemMessage(message)
        "user" -> UserMessage(message)
        "assistant" -> AssistantMessage(message)
    }
}

@Composable
private fun SystemMessage(message: ChatMessage) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 8.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = message.content,
            color = TextMuted,
            style = MaterialTheme.typography.bodySmall,
            fontStyle = FontStyle.Italic
        )
    }
}

@Composable
private fun UserMessage(message: ChatMessage) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(Surface)
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
private fun AssistantMessage(message: ChatMessage) {
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
        ChatMarkdown(content = message.content)

        // Metadata footer
        val metaParts = buildList {
            message.model?.let { add(it) }
            message.tokens?.let { add("$it tokens") }
            message.cost?.let { add("$$it") }
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

@Composable
private fun ChatMarkdown(content: String) {
    val colors = markdownColor(
        text = TextPrimary,
        codeText = TextPrimary,
        inlineCodeText = TextPrimary,
        linkText = Accent,
        codeBackground = Surface,
        inlineCodeBackground = Surface,
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
