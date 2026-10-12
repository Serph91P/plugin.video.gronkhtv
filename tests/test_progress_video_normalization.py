import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from gronkhtv_api import normalize_video


def test_normalize_video_keeps_backend_id_and_valid_watch_progress_for_account_sync():
    video = normalize_video(
        {
            "id": "b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1",
            "watch_progress": {"offset": 0, "updated_at": "2026-10-11T00:00:00Z"},
        }
    )

    assert video["id"] == "b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1"
    assert video["watch_progress"] == {"offset": 0, "updated_at": "2026-10-11T00:00:00Z"}


def test_normalize_video_does_not_treat_malformed_watch_progress_as_a_resume_value():
    assert "watch_progress" not in normalize_video({"id": "x", "watch_progress": {"offset": "invalid"}})
