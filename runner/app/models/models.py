# runner/app/models/models.py
"""
Data models for the Runner.
Defines Pydantic models for request/response schemas and data validation.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class TaskRequest(BaseModel):
    """
    Represents a task entity.

    Tasks are the fundamental units of work in the system, assigned to runners,
    and progress through various status states until completion or failure.

    Attributes:
        task_id: Universally unique identifier for the task
        etab_name: Name of the institution or organization requesting the task
        app_name: Name of client application requesting the task - used for auditing and tracking purposes
        app_version: Version of the client application requesting the task - useful for compatibility and debugging
        task_type: Category or type of work this task represents
        source_url: Location of the input data to be processed
        affiliation: Affiliation of the requester - e.g., 'student', 'employee', etc.
        parameters: Task-specific parameters required for execution
        notify_url: Callback URL for task completion notification
        completion_callback: Optional callback URL to notify upon task completion
    """

    task_id: str = Field(
        ...,
        description="Task unique identifier - automatically generated UUID for tracking task lifecycle",
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
    task_type: str = Field(..., description="Type of the task")
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
        description="Callback URL for task completion notification - if provided, ONLY the runner manager will POST the result to this URL upon task completion",
    )
    completion_callback: Optional[str] = Field(
        None,  # Default value is None for optional fields
        description="Callback URL for task completion notification - if provided, the runner will POST the result to this URL upon task completion",
    )


class TaskResultResponse(BaseModel):
    """
    Represents the result of a task operation.

    Attributes:
        status: Current state of the task in its lifecycle
    """

    status: str = Field(..., description="Status of the operation")
