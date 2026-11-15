"""Shared Pydantic v2 models for API request/response schemas.

These models define the API contract for OpenAPI spec generation.
They are NOT used for runtime validation in handlers — they serve
as the single source of truth for the generated OpenAPI spec.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkstreamState(StrEnum):
    """Workstream lifecycle states."""

    idle = "idle"
    thinking = "thinking"
    running = "running"
    attention = "attention"
    evaluation = "evaluation"
    error = "error"


class ErrorResponse(BaseModel):
    """Standard error response body."""

    error: str = Field(description="Error message")
    code: str | None = Field(
        default=None,
        description=(
            "Machine-readable reason on some refusals, e.g. ``workstream_lease_held`` "
            "(another process owns the workstream) or ``wrong_execution_node``"
        ),
    )
    holder_node_id: str | None = Field(
        default=None,
        description="With ``workstream_lease_held``: the node holding the lease, if known",
    )
    retry_after_ms: int | None = Field(
        default=None,
        description="With ``workstream_lease_held``: milliseconds until that lease expires",
    )


class StatusResponse(BaseModel):
    """Generic success response."""

    status: str = Field(default="ok", examples=["ok"])


class MemoEvictionReport(BaseModel):
    """Where a user's delegated model tokens were dropped from memory after an unlink or a
    user deletion."""

    console: str = Field(description="``evicted`` or ``failed``", examples=["evicted"])
    node_list_read: bool = Field(
        description="False when the list of nodes could not be read; every node then counts "
        "as unconfirmed"
    )
    nodes_evicted: list[str] = Field(default_factory=list)
    nodes_failed: list[str] = Field(default_factory=list)
    nodes_not_reached: list[str] = Field(default_factory=list)


class UserDeleteResponse(BaseModel):
    """DELETE /v1/api/admin/users/{user_id} response."""

    status: str = Field(
        description="``ok``, or ``incomplete`` when a process could not confirm it dropped the "
        "user's cached model tokens, or the outcome of the write is not fully known",
        examples=["ok"],
    )
    model_memo_unconfirmed: list[str] = Field(
        default_factory=list,
        description="The processes that may still hold the user's cached model tokens until "
        "they expire",
    )
    model_memo: MemoEvictionReport


class OIDCUnlinkResponse(UserDeleteResponse):
    """DELETE /v1/api/admin/oidc-identities response."""

    obo_credential_revoked: bool | None = Field(
        default=None,
        description="Whether a captured sign-in credential was deleted; null when not known",
    )
    obo_cache_rows_purged: int | None = Field(
        default=None,
        description="How many delegated token rows were deleted; null when not known",
    )


class WorkstreamDeleteRequest(BaseModel):
    """POST /v1/api/route/workstreams/delete request."""

    ws_id: str = Field(description="ID of the workstream to delete")


class WorkstreamDeleteResponse(BaseModel):
    """Success response of a workstream delete."""

    deleted: str = Field(description="ID of the workstream that was deleted")


class DeleteSettingResponse(BaseModel):
    """DELETE /v1/api/admin/settings/{key} response."""

    status: str = Field(default="ok", examples=["ok"])
    key: str = Field(description="Dotted setting key that was reset")
    default: Any = Field(description="Registry default value the setting reverted to")


class AuthLoginRequest(BaseModel):
    """POST /v1/api/auth/login request body.

    Either username+password or token must be provided.
    """

    username: str = Field(default="", description="Login username")
    password: str = Field(default="", description="Login password")
    token: str = Field(default="", description="Raw stored ts_ API token; JWT exchange is refused")


class UserAvatar(BaseModel):
    """How a page draws a person: one initial on a palette colour, or an uploaded picture."""

    color: int = Field(ge=0, le=5, description="Palette index of the generated avatar's colour")
    initial: str = Field(
        description="The letter drawn on the colour; empty when the name has no letter or digit"
    )
    picture: str | None = Field(
        default=None,
        description=(
            "Version of the uploaded picture, or null for none; the PNG is served at "
            "/v1/api/users/{user_id}/avatar?v={picture}"
        ),
    )


class AuthLoginResponse(BaseModel):
    """POST /v1/api/auth/login success response."""

    status: str = Field(default="ok")
    user_id: str = Field(default="", description="Authenticated user ID")
    username: str = Field(default="", description="Login username (user accounts)")
    display_name: str = Field(default="", description="Display name, else username (user accounts)")
    avatar: UserAvatar | None = Field(default=None, description="Avatar (user accounts)")
    role: str = Field(description="Legacy role", examples=["full", "read"])
    scopes: str = Field(
        default="", description="Comma-separated scopes", examples=["read,write,approve"]
    )
    jwt: str = Field(default="", description="JWT session token (if JWT auth is configured)")
    can_refresh: bool = Field(default=False, description="Eligible for human-session renewal")


class AuthRefreshResponse(AuthLoginResponse):
    """POST /v1/api/auth/refresh success response."""

    exp: int | None = Field(default=None, description="Session expiry in epoch seconds")


# ---------------------------------------------------------------------------
# Admin — User identity + API tokens
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    """POST /v1/api/admin/users request body."""

    username: str = Field(description="Login username (unique)")
    display_name: str = Field(description="Human-readable display name")
    password: str = Field(description="Initial password")


class UserInfo(BaseModel):
    """User record (no password_hash)."""

    user_id: str
    username: str
    display_name: str
    created: str
    avatar: UserAvatar | None = None


class ListUsersResponse(BaseModel):
    """GET /v1/api/admin/users response."""

    users: list[UserInfo]


class SetUserAvatarColorRequest(BaseModel):
    """PUT /v1/api/users/{user_id}/avatar request body."""

    color: int = Field(ge=0, le=5, description="Palette index of the avatar's colour")


class UserAvatarResponse(BaseModel):
    """The caller's avatar after a change."""

    avatar: UserAvatar


class CreateTokenRequest(BaseModel):
    """POST /v1/api/admin/users/{user_id}/tokens request body."""

    name: str = Field(default="", description="Human label for the token")
    scopes: str = Field(
        default="read,write,approve",
        description="Comma-separated scopes: read, write, approve",
    )
    expires_days: int | None = Field(
        default=None,
        description="Days until expiry (null = no expiry)",
    )


class TokenInfo(BaseModel):
    """Token metadata (never includes the hash or raw token)."""

    token_id: str
    token_prefix: str
    name: str
    scopes: str
    created: str
    expires: str | None = None


class CreateTokenResponse(BaseModel):
    """POST /v1/api/admin/users/{user_id}/tokens response (raw token shown once)."""

    token: str = Field(description="Raw API token — save this, it cannot be retrieved again")
    token_id: str
    token_prefix: str
    scopes: str


class ListTokensResponse(BaseModel):
    """GET /v1/api/admin/users/{user_id}/tokens response."""

    tokens: list[TokenInfo]


# ---------------------------------------------------------------------------
# Auth — Setup + status
# ---------------------------------------------------------------------------


class AuthSetupRequest(BaseModel):
    """POST /v1/api/auth/setup request body."""

    username: str = Field(description="Login username (1-64 ASCII characters)")
    display_name: str = Field(description="Display name")
    password: str = Field(description="Password (minimum 8 characters)")


class AuthSetupResponse(BaseModel):
    """POST /v1/api/auth/setup success response."""

    status: str = Field(default="ok")
    user_id: str
    username: str
    display_name: str = Field(default="", description="Display name, else username")
    avatar: UserAvatar | None = None
    role: str = Field(default="full")
    scopes: str = Field(default="approve,read,write")
    jwt: str = Field(default="", description="JWT session token")
    can_refresh: bool = Field(default=False, description="Eligible for human-session renewal")


class AuthStatusResponse(BaseModel):
    """GET /v1/api/auth/status response."""

    auth_enabled: bool
    has_users: bool
    setup_required: bool
    oidc_enabled: bool = False
    oidc_provider_name: str = ""
    password_enabled: bool = True


class AuthWhoamiResponse(BaseModel):
    """GET /v1/api/auth/whoami response."""

    user_id: str
    username: str = Field(
        default="",
        description=(
            "Display name, else username (user accounts); unlike login, setup and refresh, "
            "where username is the login name"
        ),
    )
    display_name: str = Field(default="", description="Display name, else username (user accounts)")
    avatar: UserAvatar | None = Field(default=None, description="Avatar (user accounts)")
    permissions: str = ""
    scopes: str = Field(default="", description="Comma-separated effective transport scopes")
    can_refresh: bool = Field(default=False, description="Eligible for human-session renewal")
    exp: int | None = Field(default=None, description="Session cookie expiry in epoch seconds")


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


class CreateScheduleRequest(BaseModel):
    """POST /v1/api/admin/schedules request body.

    A field sent as null is rejected with a 400 naming the field; omit a
    field to take its default.
    """

    name: str = Field(description="Human-readable schedule name")
    description: str = Field(default="", description="Optional description")
    schedule_type: str = Field(description="'cron' or 'at'")
    cron_expr: str = Field(default="", description="Cron expression (when schedule_type='cron')")
    at_time: str = Field(default="", description="ISO8601 timestamp (when schedule_type='at')")
    timezone: str = Field(
        default="UTC",
        description="IANA zone the cron is evaluated in (e.g. America/New_York); 'at' ignores it",
    )
    target_mode: str = Field(default="auto", description="auto, pool, all, or specific node_id")
    model: str = Field(default="", description="Model alias for the workstream")
    initial_message: str = Field(description="Message sent to the new workstream")
    auto_approve: bool = Field(default=False)
    auto_approve_tools: list[str] = Field(default_factory=list)
    skill: str = Field(default="", description="Skill name")
    persona: str = Field(default="", description="Persona slug (empty = kind default)")
    project_id: str = Field(default="", description="Project to attach the workstream to")
    notify_targets: list[dict[str, str]] = Field(
        default_factory=list,
        description="Notification targets on completion (channel_type + channel_id/user_id)",
    )
    enabled: bool = Field(default=True)


class UpdateScheduleRequest(BaseModel):
    """PUT /v1/api/admin/schedules/{task_id} request body (partial update).

    Omit a field to leave it unchanged; a field sent as null is rejected with
    a 400 naming the field, and a timing field resent with its stored value
    is not a change.
    """

    name: str | None = None
    description: str | None = None
    schedule_type: str | None = None
    cron_expr: str | None = None
    at_time: str | None = None
    timezone: str | None = None
    target_mode: str | None = None
    model: str | None = None
    initial_message: str | None = None
    auto_approve: bool | None = None
    auto_approve_tools: list[str] | None = None
    skill: str | None = None
    persona: str | None = None
    project_id: str | None = None
    notify_targets: list[dict[str, str]] | None = None
    enabled: bool | None = None


class ScheduleInfo(BaseModel):
    """Scheduled task details."""

    task_id: str
    name: str
    description: str = ""
    schedule_type: str
    cron_expr: str = ""
    at_time: str = ""
    timezone: str = "UTC"
    target_mode: str = "auto"
    model: str = ""
    initial_message: str
    auto_approve: bool = False
    auto_approve_tools: list[str] = Field(default_factory=list)
    skill: str = ""
    persona: str = ""
    project_id: str = ""
    notify_targets: list[dict[str, str]] = Field(default_factory=list)
    enabled: bool = True
    created_by: str = ""
    last_run: str | None = None
    next_run: str | None = None
    created: str = ""
    updated: str = ""


class ListSchedulesResponse(BaseModel):
    """GET /v1/api/admin/schedules response."""

    schedules: list[ScheduleInfo]


class ScheduleRunInfo(BaseModel):
    """Single execution record for a scheduled task."""

    run_id: str
    task_id: str
    node_id: str = ""
    ws_id: str = ""
    correlation_id: str = ""
    started: str
    status: str = "dispatched"
    error: str = ""


class ListScheduleRunsResponse(BaseModel):
    """GET /v1/api/admin/schedules/{task_id}/runs response."""

    runs: list[ScheduleRunInfo]
