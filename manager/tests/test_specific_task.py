"""Task-level persistence tests."""

from datetime import datetime

from app.core.persistence import SafeDailyJSONPersistence
from app.models.models import Task


def test_save_and_load_round_trip(tmp_path):
    persistence = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=1)

    task = Task(
        task_id="task-abc",
        runner_id="runner-1",
        status="running",
        etab_name="test_etab",
        app_name="test_app",
        app_version="1.0.0",
        task_type="video",
        source_url="http://example.com/source",
        affiliation="qa",
        parameters={"quality": "1080p"},
        notify_url="http://example.com/notify",
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )

    assert persistence.save_tasks({task.task_id: task})

    loaded = persistence.load_tasks(load_all=False)

    assert task.task_id in loaded
    assert loaded[task.task_id]["status"] == "running"
    assert "_metadata" not in loaded[task.task_id]
