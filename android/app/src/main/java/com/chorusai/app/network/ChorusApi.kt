package com.chorusai.app.network

import com.chorusai.app.model.*
import okhttp3.MultipartBody
import retrofit2.Response
import retrofit2.http.*

interface ChorusApi {

    // --- Auth ---

    @POST("api/login")
    suspend fun login(@Body request: LoginRequest): Response<LoginResponse>

    @POST("api/set-api-key")
    suspend fun setApiKey(@Body request: ApiKeyRequest): Response<Map<String, String>>

    @POST("api/set-api-keys")
    suspend fun setApiKeys(@Body request: ApiKeysRequest): Response<ApiKeysResponse>

    @GET("api/api-keys/{username}")
    suspend fun getApiKeys(@Path("username") username: String): Response<ApiKeysResponse>

    // --- Models & Config ---

    @GET("api/models")
    suspend fun getModels(): Response<List<ModelInfo>>

    @POST("api/set-chat-model")
    suspend fun setChatModel(@Body request: SetChatModelRequest): Response<Map<String, String>>

    @POST("api/set-project-model")
    suspend fun setProjectModel(@Body request: SetProjectModelRequest): Response<Map<String, String>>

    @GET("api/game-systems")
    suspend fun getGameSystems(): Response<List<GameSystemInfo>>

    @POST("api/set-project-game-system")
    suspend fun setProjectGameSystem(@Body request: SetProjectGameSystemRequest): Response<Map<String, String>>

    // --- Chat Management ---

    @GET("api/chats/{username}")
    suspend fun getChats(
        @Path("username") username: String,
        @Query("limit") limit: Int = 20,
        @Query("offset") offset: Int = 0
    ): Response<ChatListResponse>

    @POST("api/create-chat")
    suspend fun createChat(@Body request: CreateChatRequest): Response<Map<String, String>>

    @GET("api/chat/{username}/{chat_name}")
    suspend fun getChat(
        @Path("username") username: String,
        @Path("chat_name") chatName: String,
        @Query("project") project: String? = null,
        @Query("leaf_id") leafId: String? = null,
        @Query("limit") limit: Int = 30,
        @Query("offset") offset: Int = 0
    ): Response<ChatResponse>

    @POST("api/send-message")
    suspend fun sendMessage(@Body request: SendMessageRequest): Response<MessageResponse>

    @POST("api/rename-chat")
    suspend fun renameChat(@Body request: RenameChatRequest): Response<Map<String, String>>

    @POST("api/delete-chat")
    suspend fun deleteChat(@Body request: DeleteChatRequest): Response<Map<String, String>>

    // --- Branching ---

    @GET("api/branch-info/{username}/{chat_name}")
    suspend fun getBranchInfo(
        @Path("username") username: String,
        @Path("chat_name") chatName: String,
        @Query("message_id") messageId: String,
        @Query("project") project: String? = null
    ): Response<BranchInfoResponse>

    @POST("api/switch-branch/{username}/{chat_name}")
    suspend fun switchBranch(
        @Path("username") username: String,
        @Path("chat_name") chatName: String,
        @Query("target_message_id") targetMessageId: String,
        @Query("project") project: String? = null
    ): Response<Map<String, String>>

    @POST("api/set-bookmark")
    suspend fun setBookmark(@Body request: SetBookmarkRequest): Response<Map<String, Any>>

    // --- Chat Settings ---

    @POST("api/set-anthropic-sync")
    suspend fun setAnthropicSync(@Body request: SetAnthropicSyncRequest): Response<Map<String, Any>>

    @POST("api/reload-chat")
    suspend fun reloadChat(@Body request: ReloadChatRequest): Response<Map<String, Any>>

    // --- Updates & Tokens ---

    @GET("api/updates/{username}/{chat_name}")
    suspend fun getUpdates(
        @Path("username") username: String,
        @Path("chat_name") chatName: String,
        @Query("project") project: String? = null
    ): Response<Map<String, String>>

    @POST("api/save-updates")
    suspend fun saveUpdates(@Body request: SaveUpdatesRequest): Response<Map<String, Any>>

    @POST("api/count-tokens")
    suspend fun countTokens(@Body request: CountTokensRequest): Response<Map<String, Int>>

    // --- Projects ---

    @POST("api/create-project")
    suspend fun createProject(@Body request: CreateProjectRequest): Response<Map<String, String>>

    @POST("api/rename-project")
    suspend fun renameProject(@Body request: RenameProjectRequest): Response<Map<String, String>>

    @POST("api/delete-project")
    suspend fun deleteProject(@Body request: DeleteProjectRequest): Response<Map<String, String>>

    @GET("api/project-metadata/{username}/{project}")
    suspend fun getProjectMetadata(
        @Path("username") username: String,
        @Path("project") project: String
    ): Response<ProjectMetadata>

    // --- Project Chats ---

    @GET("api/project-chats/{username}/{project}")
    suspend fun getProjectChats(
        @Path("username") username: String,
        @Path("project") project: String,
        @Query("limit") limit: Int = 20,
        @Query("offset") offset: Int = 0
    ): Response<ProjectChatsResponse>

    @GET("api/project-chats-detailed/{username}/{project}")
    suspend fun getProjectChatsDetailed(
        @Path("username") username: String,
        @Path("project") project: String,
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0
    ): Response<ProjectChatsDetailedResponse>

    // --- Project Files ---

    @GET("api/project-files/{username}/{project}")
    suspend fun getProjectFiles(
        @Path("username") username: String,
        @Path("project") project: String,
        @Query("model") model: String? = null
    ): Response<ProjectFilesResponse>

    @Multipart
    @POST("api/project-files/{username}/{project}")
    suspend fun uploadProjectFiles(
        @Path("username") username: String,
        @Path("project") project: String,
        @Part files: List<MultipartBody.Part>
    ): Response<Map<String, Any>>

    @PATCH("api/project-files/{username}/{project}/staged/{filename}")
    suspend fun updateFileStaged(
        @Path("username") username: String,
        @Path("project") project: String,
        @Path("filename") filename: String,
        @Body request: UpdateStagedRequest
    ): Response<Map<String, Any>>

    @PUT("api/project-files/{username}/{project}/agents/{filename}")
    suspend fun updateFileAgents(
        @Path("username") username: String,
        @Path("project") project: String,
        @Path("filename") filename: String,
        @Body request: UpdateFileAgentsRequest
    ): Response<Map<String, Any>>

    @DELETE("api/project-files/{username}/{project}/{filename}")
    suspend fun deleteProjectFile(
        @Path("username") username: String,
        @Path("project") project: String,
        @Path("filename") filename: String
    ): Response<Map<String, String>>

    // --- Project Instructions ---

    @GET("api/project-instructions/{username}/{project}")
    suspend fun getProjectInstructions(
        @Path("username") username: String,
        @Path("project") project: String,
        @Query("model") model: String? = null
    ): Response<ProjectInstructionsResponse>

    @PUT("api/project-instructions/{username}/{project}")
    suspend fun updateProjectInstructions(
        @Path("username") username: String,
        @Path("project") project: String,
        @Body request: UpdateInstructionsRequest
    ): Response<Map<String, Any>>

    @GET("api/project-instructions/{username}/{project}/agents")
    suspend fun getAgentInstructions(
        @Path("username") username: String,
        @Path("project") project: String
    ): Response<Map<String, ProjectInstructionsResponse>>

    @PUT("api/project-instructions/{username}/{project}/agents/{agent_name}")
    suspend fun updateAgentInstructions(
        @Path("username") username: String,
        @Path("project") project: String,
        @Path("agent_name") agentName: String,
        @Body request: UpdateInstructionsRequest
    ): Response<Map<String, Any>>

    // --- Stats ---

    @GET("api/user-stats/{username}")
    suspend fun getUserStats(@Path("username") username: String): Response<UserStatsResponse>

    @GET("api/free-tokens/{username}")
    suspend fun getFreeTokens(@Path("username") username: String): Response<FreeTokensResponse>

    // --- Character Sheet ---

    @GET("api/character-sheet/{username}/{project}")
    suspend fun getCharacterSheet(
        @Path("username") username: String,
        @Path("project") project: String
    ): Response<Map<String, String>>

    // --- Health ---

    @GET("health")
    suspend fun healthCheck(): Response<Map<String, String>>
}
