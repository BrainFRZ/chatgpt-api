package com.chorusai.app.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performTextInput
import androidx.navigation.compose.rememberNavController
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chorusai.app.ui.screens.ApiKeySetupScreen
import com.chorusai.app.ui.theme.ChorusTheme
import dagger.hilt.android.testing.HiltAndroidRule
import dagger.hilt.android.testing.HiltAndroidTest
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@HiltAndroidTest
@RunWith(AndroidJUnit4::class)
class ApiKeySetupFlowTest {

    @get:Rule(order = 0)
    val hiltRule = HiltAndroidRule(this)

    @get:Rule(order = 1)
    val composeRule = createComposeRule()

    @Before
    fun setup() {
        hiltRule.inject()
    }

    @Test
    fun apiKeySetup_displaysTitle() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("Set Up API Keys").assertIsDisplayed()
    }

    @Test
    fun apiKeySetup_displaysNewUserSubtitle() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("Enter at least one API key to get started").assertIsDisplayed()
    }

    @Test
    fun apiKeySetup_displaysReturningUserSubtitle() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = false,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("API Key Configuration").assertIsDisplayed()
    }

    @Test
    fun apiKeySetup_displaysOpenAIField() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("OpenAI Key").assertIsDisplayed()
    }

    @Test
    fun apiKeySetup_displaysAnthropicField() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("Anthropic Key").assertIsDisplayed()
    }

    @Test
    fun apiKeySetup_displaysSaveButton() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("Save").assertIsDisplayed()
    }

    @Test
    fun saveButton_disabledWhenBothFieldsEmpty() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("Save").assertIsNotEnabled()
    }

    @Test
    fun saveButton_enabledWhenOpenAIKeyEntered() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("OpenAI Key").performTextInput("sk-test123")
        composeRule.onNodeWithText("Save").assertIsEnabled()
    }

    @Test
    fun saveButton_enabledWhenAnthropicKeyEntered() {
        composeRule.setContent {
            ChorusTheme {
                ApiKeySetupScreen(
                    username = "testuser",
                    isNewUser = true,
                    navController = rememberNavController()
                )
            }
        }

        composeRule.onNodeWithText("Anthropic Key").performTextInput("sk-ant-test")
        composeRule.onNodeWithText("Save").assertIsEnabled()
    }
}
