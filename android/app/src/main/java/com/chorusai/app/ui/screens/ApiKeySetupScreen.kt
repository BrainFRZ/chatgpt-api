package com.chorusai.app.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavController
import com.chorusai.app.ui.navigation.Screen
import com.chorusai.app.ui.theme.Accent
import com.chorusai.app.ui.theme.Error
import com.chorusai.app.ui.theme.Success
import com.chorusai.app.ui.theme.Surface
import com.chorusai.app.ui.theme.TextSecondary
import com.chorusai.app.ui.viewmodel.AuthViewModel
import com.chorusai.app.ui.viewmodel.NavigationEvent

@Composable
fun ApiKeySetupScreen(
    username: String,
    isNewUser: Boolean,
    navController: NavController,
    viewModel: AuthViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val keyboardController = LocalSoftwareKeyboardController.current
    var openaiKey by rememberSaveable { mutableStateOf("") }
    var anthropicKey by rememberSaveable { mutableStateOf("") }
    var openaiVisible by rememberSaveable { mutableStateOf(false) }
    var anthropicVisible by rememberSaveable { mutableStateOf(false) }

    val hasExistingKeys = uiState.hasOpenai || uiState.hasAnthropic

    LaunchedEffect(username) {
        viewModel.loadApiKeyStatus(username)
    }

    LaunchedEffect(Unit) {
        viewModel.navigationEvents.collect { event ->
            when (event) {
                is NavigationEvent.NavigateToChatList -> {
                    navController.navigate(Screen.ChatList.route) {
                        popUpTo(Screen.ApiKeySetup.route) { inclusive = true }
                    }
                }
                else -> {}
            }
        }
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .systemBarsPadding(),
        contentAlignment = Alignment.Center
    ) {
        Card(
            modifier = Modifier
                .widthIn(max = 400.dp)
                .padding(24.dp),
            colors = CardDefaults.cardColors(containerColor = Surface),
            shape = RoundedCornerShape(16.dp)
        ) {
            Column(
                modifier = Modifier.padding(24.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Text(
                    text = "Set Up API Keys",
                    style = MaterialTheme.typography.headlineLarge,
                    color = MaterialTheme.colorScheme.onBackground
                )

                Spacer(modifier = Modifier.height(8.dp))

                Text(
                    text = if (isNewUser) "Enter at least one API key to get started"
                    else "API Key Configuration",
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextSecondary
                )

                Spacer(modifier = Modifier.height(24.dp))

                // OpenAI key field
                OutlinedTextField(
                    value = openaiKey,
                    onValueChange = { openaiKey = it },
                    label = {
                        Text(
                            if (uiState.hasOpenai) "OpenAI Key (configured)"
                            else "OpenAI Key"
                        )
                    },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    visualTransformation = if (openaiVisible) VisualTransformation.None
                    else PasswordVisualTransformation(),
                    trailingIcon = {
                        if (uiState.hasOpenai && openaiKey.isBlank()) {
                            Icon(
                                imageVector = Icons.Default.Check,
                                contentDescription = "Configured",
                                tint = Success,
                                modifier = Modifier.size(20.dp)
                            )
                        } else {
                            IconButton(onClick = { openaiVisible = !openaiVisible }) {
                                Icon(
                                    imageVector = if (openaiVisible) Icons.Default.VisibilityOff
                                    else Icons.Default.Visibility,
                                    contentDescription = if (openaiVisible) "Hide" else "Show",
                                    tint = TextSecondary
                                )
                            }
                        }
                    },
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = Accent,
                        unfocusedBorderColor = TextSecondary,
                        cursorColor = Accent,
                        focusedLabelColor = Accent
                    )
                )

                Spacer(modifier = Modifier.height(16.dp))

                // Anthropic key field
                OutlinedTextField(
                    value = anthropicKey,
                    onValueChange = { anthropicKey = it },
                    label = {
                        Text(
                            if (uiState.hasAnthropic) "Anthropic Key (configured)"
                            else "Anthropic Key"
                        )
                    },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    visualTransformation = if (anthropicVisible) VisualTransformation.None
                    else PasswordVisualTransformation(),
                    trailingIcon = {
                        if (uiState.hasAnthropic && anthropicKey.isBlank()) {
                            Icon(
                                imageVector = Icons.Default.Check,
                                contentDescription = "Configured",
                                tint = Success,
                                modifier = Modifier.size(20.dp)
                            )
                        } else {
                            IconButton(onClick = { anthropicVisible = !anthropicVisible }) {
                                Icon(
                                    imageVector = if (anthropicVisible) Icons.Default.VisibilityOff
                                    else Icons.Default.Visibility,
                                    contentDescription = if (anthropicVisible) "Hide" else "Show",
                                    tint = TextSecondary
                                )
                            }
                        }
                    },
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = Accent,
                        unfocusedBorderColor = TextSecondary,
                        cursorColor = Accent,
                        focusedLabelColor = Accent
                    )
                )

                if (uiState.error != null) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = uiState.error!!,
                        color = Error,
                        style = MaterialTheme.typography.bodySmall
                    )
                }

                Spacer(modifier = Modifier.height(24.dp))

                Button(
                    onClick = {
                        keyboardController?.hide()
                        viewModel.saveApiKeys(username, openaiKey, anthropicKey)
                    },
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(48.dp),
                    enabled = !uiState.isLoading && (
                            openaiKey.isNotBlank() || anthropicKey.isNotBlank() || hasExistingKeys
                            ),
                    colors = ButtonDefaults.buttonColors(containerColor = Accent),
                    shape = RoundedCornerShape(8.dp)
                ) {
                    if (uiState.isLoading) {
                        CircularProgressIndicator(
                            color = MaterialTheme.colorScheme.onPrimary,
                            strokeWidth = 2.dp,
                            modifier = Modifier.size(20.dp)
                        )
                    } else {
                        Text("Save")
                    }
                }

                if (hasExistingKeys) {
                    Spacer(modifier = Modifier.height(8.dp))
                    TextButton(
                        onClick = {
                            navController.navigate(Screen.ChatList.route) {
                                popUpTo(Screen.ApiKeySetup.route) { inclusive = true }
                            }
                        }
                    ) {
                        Text("Skip", color = TextSecondary)
                    }
                }
            }
        }
    }
}
