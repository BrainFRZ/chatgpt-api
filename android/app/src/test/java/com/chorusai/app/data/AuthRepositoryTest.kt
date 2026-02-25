package com.chorusai.app.data

import com.chorusai.app.model.ApiKeysRequest
import com.chorusai.app.model.ApiKeysResponse
import com.chorusai.app.model.LoginRequest
import com.chorusai.app.model.LoginResponse
import com.chorusai.app.network.ChorusApi
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.Response

class AuthRepositoryTest {

    private lateinit var api: ChorusApi
    private lateinit var prefs: UserPreferences
    private lateinit var repo: AuthRepository

    @Before
    fun setup() {
        api = mockk(relaxed = true)
        prefs = mockk(relaxed = true)
        repo = AuthRepository(api, prefs)
    }

    @Test
    fun `login delegates to api with LoginRequest`() = runTest {
        val expected = LoginResponse("testuser", hasApiKey = true, isNewUser = false)
        coEvery { api.login(LoginRequest("testuser")) } returns Response.success(expected)

        val response = repo.login("testuser")

        assertTrue(response.isSuccessful)
        assertEquals(expected, response.body())
        coVerify { api.login(LoginRequest("testuser")) }
    }

    @Test
    fun `setApiKeys delegates to api with ApiKeysRequest`() = runTest {
        val expected = ApiKeysResponse(hasOpenai = true, hasAnthropic = false)
        coEvery {
            api.setApiKeys(ApiKeysRequest("testuser", "sk-abc", null))
        } returns Response.success(expected)

        val response = repo.setApiKeys("testuser", "sk-abc", null)

        assertTrue(response.isSuccessful)
        assertEquals(expected, response.body())
    }

    @Test
    fun `setApiKeys sends both keys when provided`() = runTest {
        val expected = ApiKeysResponse(hasOpenai = true, hasAnthropic = true)
        coEvery {
            api.setApiKeys(ApiKeysRequest("testuser", "sk-openai", "sk-anthropic"))
        } returns Response.success(expected)

        val response = repo.setApiKeys("testuser", "sk-openai", "sk-anthropic")

        assertTrue(response.isSuccessful)
        coVerify {
            api.setApiKeys(ApiKeysRequest("testuser", "sk-openai", "sk-anthropic"))
        }
    }

    @Test
    fun `getApiKeys delegates to api`() = runTest {
        val expected = ApiKeysResponse(hasOpenai = false, hasAnthropic = true)
        coEvery { api.getApiKeys("testuser") } returns Response.success(expected)

        val response = repo.getApiKeys("testuser")

        assertTrue(response.isSuccessful)
        assertEquals(expected, response.body())
        coVerify { api.getApiKeys("testuser") }
    }

    @Test
    fun `saveLogin delegates to prefs`() = runTest {
        repo.saveLogin("testuser")

        coVerify { prefs.saveLogin("testuser") }
    }

    @Test
    fun `logout delegates to prefs`() = runTest {
        repo.logout()

        coVerify { prefs.logout() }
    }
}
