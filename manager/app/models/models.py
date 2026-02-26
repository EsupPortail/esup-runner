# runner_manager/models.py
"""
Data models for the Runner Manager API.
Defines Pydantic models for request/response schemas and data validation.
"""

from datetime import datetime
from ipaddress import ip_address
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def _validate_safe_url(url: str, field_name: str) -> str:
    """Validate that a URL is safe against SSRF attacks.

    Rejects:
    - Non HTTP/HTTPS schemes
    - Private/loopback/link-local IP addresses
    - Cloud metadata endpoints (169.254.169.254)

    Args:
        url: The URL to validate
        field_name: Name of the field for error messages

    Returns:
        The validated URL

    Raises:
        ValueError: If the URL is considered unsafe
    """
    if not url:
        return url

    parsed = urlparse(url)

    # Only allow http and https schemes
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field_name} must use http or https scheme")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"{field_name} must have a valid hostname")

    # Check for private/reserved IP addresses
    try:
        addr = ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(f"{field_name} must not point to a private or reserved IP address")
    except ValueError as e:
        # If it's our own error, re-raise; otherwise it's a hostname (not an IP), which is fine
        if "must not point" in str(e) or "must use" in str(e) or "must have" in str(e):
            raise

    return url


class Runner(BaseModel):
    """
    Represents a runner instance that can execute tasks.

    A runner is a worker node that registers with the manager and becomes
    available to process distributed tasks. Each runner has a unique ID
    and a communication URL where it can receive task requests.

    Attributes:
        id: Unique identifier for the runner
        url: Base URL where the runner API is accessible (including protocol and port)
        task_types: Task types managed by this runner
        status: Runner status (online, offline) - tracks runner progression through workflow states
        availability: Availability status - indicates if the runner is ready to accept new tasks (available) or busy (busy)
        last_heartbeat: Timestamp of the last heartbeat received - used to determine runner health and connectivity status
        token: Authentication token used for secure communication with the manager
        version: Runner software version - useful for compatibility checks and debugging
    """

    id: str = Field(
        ...,
        description="Unique runner identifier - used to track and communicate with specific runner instances",
    )
    url: str = Field(
        ...,
        description="Runner URL for communication - must be accessible by the manager for task distribution and health checks",
    )
    task_types: List[str] = Field(
        default_factory=list, description="Task types managed by this runner"
    )
    status: str = Field(
        "online",
        description="Runner status: online, offline - tracks runner progression through workflow states",
    )
    availability: str = Field(
        "available",
        description="Availability status - indicates if the runner is ready to accept new tasks (available) or busy (busy)",
    )
    last_heartbeat: datetime = Field(
        datetime.now(),
        description="Timestamp of the last heartbeat received - used to determine runner health and connectivity status",
    )
    token: Optional[str] = Field(
        None,
        description="Authentication token for secure API communication - ensures only authorized runners can register and receive tasks",
    )
    version: Optional[str] = Field(
        None, description="Runner software version - useful for compatibility checks and debugging"
    )


class TaskRequest(BaseModel):
    """
    Request model for creating and executing a new task.

    This model is used when submitting a task for execution. It contains
    the necessary information to identify what type of work needs to be done.

    Attributes:
        etab_name: Name of the institution or organization requesting the task
        app_name: Name of client application requesting the task - used for auditing and tracking purposes
        app_version: Version of the client application requesting the task - useful for compatibility and debugging
        task_type: Category or type of work this task represents
        source_url: Location of the input data to be processed
        affiliation: Affiliation of the requester - e.g., 'student', 'employee', etc.
        parameters: Task-specific parameters required for execution
        notify_url: Callback URL for task completion notification
    """

    etab_name: str = Field(
        ..., description="Name of the institution or organization requesting the task"
    )
    app_name: str = Field(
        ...,
        description="Name of client application requesting the task - used for auditing and tracking purposes",
    )
    app_version: Optional[str] = Field(
        None,
        description="Version of the client application requesting the task - useful for compatibility and debugging",
    )
    task_type: str = Field(
        ...,
        description="Type of the task - copied from the original TaskRequest to maintain task classification",
    )
    source_url: str = Field(
        ..., description="Source URL for the task - location of the input data to be processed"
    )
    affiliation: Optional[str] = Field(
        None, description="Affiliation of the requester - e.g., 'student', 'employee', etc."
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Task parameters - task-specific parameters required for execution",
    )
    notify_url: str = Field(
        ...,
        description="Callback URL for task completion notification - if provided, the runner manager will POST the result to this URL upon task completion",
    )

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, v: str) -> str:
        return _validate_safe_url(v, "source_url")

    @field_validator("notify_url")
    @classmethod
    def validate_notify_url(cls, v: str) -> str:
        return _validate_safe_url(v, "notify_url")


class Task(BaseModel):
    """
    Represents a task entity with its lifecycle and execution details.

    Tasks are the fundamental units of work in the system. They are created
    when a TaskRequest is submitted, assigned to runners, and progress through
    various status states until completion or failure.

    Attributes:
        task_id: Universally unique identifier for the task
        runner_id: ID of the runner currently assigned to execute this task
        status: Current state of the task in its lifecycle
        etab_name: Name of the institution or organization requesting the task
        app_name: Name of client application requesting the task - used for auditing and tracking purposes
        app_version: Version of the client application requesting the task - useful for compatibility and debugging
        task_type: Category or type of work this task represents
        source_url: Location of the input data to be processed
        affiliation: Affiliation of the requester - e.g., 'student', 'employee', etc.
        parameters: Task-specific parameters required for execution
        notify_url: Callback URL for task completion notification
        created_at: ISO timestamp when the task was initially created
        updated_at: ISO timestamp when the task was last modified
        error: Optional error message if the task failed during execution
        script_output: Optional raw output from the task execution script - useful for debugging and understanding task behavior
    """

    task_id: str = Field(
        ...,
        description="Task unique identifier - automatically generated UUID for tracking task lifecycle",
    )
    runner_id: str = Field(
        ..., description="ID of runner executing the task - references the Runner model ID"
    )
    status: str = Field(
        ...,
        description="Task status: pending, running, completed, failed, timeout - tracks task progression through workflow states",
    )
    etab_name: str = Field(
        ..., description="Name of the institution or organization requesting the task"
    )
    app_name: str = Field(
        ...,
        description="Name of client application requesting the task - used for auditing and tracking purposes",
    )
    app_version: Optional[str] = Field(
        None,
        description="Version of the client application requesting the task - useful for compatibility and debugging",
    )
    task_type: str = Field(
        ...,
        description="Type of the task - copied from the original TaskRequest to maintain task classification",
    )
    source_url: str = Field(
        ..., description="Source URL for the task - location of the input data to be processed"
    )
    affiliation: Optional[str] = Field(
        None, description="Affiliation of the requester - e.g., 'student', 'employee', etc."
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Task parameters - task-specific parameters required for execution",
    )
    notify_url: str = Field(
        ...,
        description="Callback URL for task completion notification - if provided, the runner manager will POST the result to this URL upon task completion",
    )
    client_token: Optional[str] = Field(
        None,
        description=(
            "Bearer token provided by the client when submitting the task. "
            "Forwarded by the manager to notify_url callbacks."
        ),
    )
    completion_callback: Optional[str] = Field(
        None,
        description="Callback URL for task completion notification - if provided, the runner will POST the result to this URL upon task completion",
    )
    run_id: Optional[str] = Field(
        None,
        description=(
            "Manager-side execution identifier for this task record. "
            "Changes at each (re)execution to avoid stale async updates."
        ),
    )
    created_at: str = Field(
        ...,
        description="Task creation timestamp in ISO format - used for auditing and cleanup of old tasks",
    )
    updated_at: str = Field(
        ...,
        description="Task last update timestamp in ISO format - tracks when status changes occur for monitoring",
    )
    error: Optional[str] = Field(
        None,  # Default value is None for optional fields
        description="Error message if failed or warning - provides diagnostic information when tasks don't complete successfully",
    )
    script_output: Optional[str] = Field(
        None,
        description="Raw output from the task execution script - useful for debugging and understanding task behavior",
    )


class TaskResultManifest(BaseModel):
    """
    Manifest model listing files produced by a task.

    Attributes:
        task_id: Task identifier
        files: List of file paths produced for the task
    """

    task_id: str = Field(..., description="Task identifier")
    files: List[str] = Field(
        default_factory=list,
        description="List of files produced by the task, relative to the task result directory",
    )


class TaskCompletionNotification(BaseModel):
    """
    Notification model for task completion events from runners to manager.

    This model is used when runners notify the manager about task completion
    or failure. It serves as a callback mechanism to update task status
    without requiring continuous polling from the manager.

    Attributes:
        task_id: Reference to the task being reported on
        status: Final status of the task (completed, warning or failed)
        error_message: Detailed error information if the task has a warning or is failed
        script_output: Raw output from the task execution script - useful for debugging and understanding task behavior
    """

    task_id: str = Field(
        ...,
        description="Task identifier - must match the ID of the originally assigned task for proper status updates",
    )
    status: str = Field(
        ...,
        description="Task status: 'completed', 'warning' or 'failed' - indicates final execution outcome from runner perspective",
    )
    error_message: Optional[str] = Field(
        None,
        description="Error message if failed or warning - provides specific failure details for debugging and monitoring purposes",
    )
    script_output: Optional[str] = Field(
        None,
        description="Raw output from the task execution script - useful for debugging and understanding task behavior",
    )
