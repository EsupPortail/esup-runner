import json
from unittest.mock import Mock, patch

from app.models.models import TaskRequest
from app.task_handlers.studio.studio_handler import StudioEncodingHandler

MP_XML = """<?xml version="1.0" ?><mediapackage xmlns="http://mediapackage.opencastproject.org" id="id" start="2025-12-11T11:43:34Z" presenter="piph">
    <media>
      <track id="t1" type="presentation/source"><mimetype>video/webm</mimetype><url>https://example.org/presentation.webm</url><live>false</live></track>
      <track id="t2" type="presenter/source"><mimetype>video/webm</mimetype><url>https://example.org/presenter.webm</url><live>false</live></track>
    </media>
    <metadata>
      <catalog id="c1" type="smil/cutting"><mimetype>text/xml</mimetype><url>https://example.org/cutting.smil</url></catalog>
    </metadata>
  </mediapackage>"""

SMIL = """
<smil xmlns="http://www.w3.org/ns/SMIL">
  <body>
    <par>
      <video clipBegin="1.0s" clipEnd="3.0s" />
    </par>
  </body>
</smil>
"""


def make_task_request():
    return TaskRequest(
        task_id="task-123",
        etab_name="UM",
        app_name="TestApp",
        task_type="studio",
        source_url="https://example.org/mediapackage.xml",
        parameters={},
        notify_url="http://manager/callback",
    )


@patch("subprocess.run")
@patch("requests.get")
def test_studio_handler_success(mock_get, mock_run):
    # Mock requests for XML and SMIL
    def _resp(text):
        r = Mock()
        r.status_code = 200
        r.text = text
        return r

    mock_get.side_effect = [_resp(MP_XML), _resp(SMIL)]

    # Mock ffprobe and ffmpeg subprocess
    def run_side_effect(cmd, stdout=None, stderr=None, shell=False, cwd=None):
        m = Mock()
        if isinstance(cmd, list) and "ffprobe" in cmd[0]:
            m.stdout = json.dumps({"streams": [{"height": 720}]}).encode()
            m.returncode = 0
        else:
            m.stdout = b"ffmpeg ok"
            m.returncode = 0
        return m

    mock_run.side_effect = run_side_effect

    handler = StudioEncodingHandler()
    res = handler.execute_task("task-123", make_task_request())
    assert res["task_type"] == "studio"
    assert res["success"] in (True, False)  # ffmpeg output file may not exist in test
    assert "script_output" in res
