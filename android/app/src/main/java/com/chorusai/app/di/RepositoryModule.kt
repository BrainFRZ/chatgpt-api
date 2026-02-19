package com.chorusai.app.di

import com.chorusai.app.data.AuthRepository
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.ProjectRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.network.ChorusApi
import com.chorusai.app.network.SseClient
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object RepositoryModule {

    @Provides
    @Singleton
    fun provideAuthRepository(api: ChorusApi, prefs: UserPreferences): AuthRepository =
        AuthRepository(api, prefs)

    @Provides
    @Singleton
    fun provideChatRepository(api: ChorusApi, sseClient: SseClient): ChatRepository =
        ChatRepository(api, sseClient)

    @Provides
    @Singleton
    fun provideProjectRepository(api: ChorusApi): ProjectRepository =
        ProjectRepository(api)
}
