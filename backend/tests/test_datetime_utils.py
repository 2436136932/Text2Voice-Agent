"""AwareUTC must mark naive datetimes as UTC across serialization paths.

Regression: history timestamps were serialized bare (``2026-10-13T10:01:22``),
so the browser parsed the UTC value as *local* time and displayed every record
8 hours off for UTC+8 users.
"""

from datetime import datetime, timezone

from pydantic import BaseModel

from app.schemas.datetime_utils import AwareUTC


class Demo(BaseModel):
    created_at: AwareUTC | None = None


class TestAwareUTC:
    def test_naive_value_is_marked_utc_in_json(self):
        demo = Demo(created_at=datetime(2026, 10, 13, 10, 1, 22))
        assert "10:01:22+00:00" in demo.model_dump_json()

    def test_aware_value_is_preserved(self):
        aware = datetime(2026, 10, 13, 18, 1, 22, tzinfo=timezone.utc)
        demo = Demo(created_at=aware)
        assert aware.isoformat() in demo.model_dump_json()

    def test_json_dump_mode_also_marks(self):
        """``model_dump(mode='json')`` (used by some FastAPI paths) too."""
        demo = Demo(created_at=datetime(2026, 10, 13, 10, 1, 22))
        assert demo.model_dump(mode="json")["created_at"].endswith("+00:00")

    def test_none_stays_none(self):
        assert Demo(created_at=None).model_dump(mode="json")["created_at"] is None

    def test_validation_output_is_aware(self):
        """The marker is applied at validation, so Python code sees aware values."""
        demo = Demo(created_at=datetime(2026, 10, 13, 10, 1, 22))
        assert demo.created_at is not None
        assert demo.created_at.tzinfo is not None
