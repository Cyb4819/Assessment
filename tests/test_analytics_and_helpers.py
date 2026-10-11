import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pymongo.errors import ServerSelectionTimeoutError

import app.main as main
from app.main import build_department_trend_pipeline, build_history_length_filter, parse_calendar_date


def test_department_trend_seeds_empty_date_ranges_and_uses_half_up_rounding():
    pipeline = build_department_trend_pipeline("Engineering", "2026-07-06", "2026-07-15")
    stages = [stage for stage in pipeline if isinstance(stage, dict)]
    union_stage = next(stage["$unionWith"] for stage in stages if "$unionWith" in stage)

    assert union_stage["coll"] == "employees"
    assert union_stage["pipeline"][0] == {"$match": {"department": "Engineering"}}
    assert any("$densify" in stage for stage in stages)
    assert "$round" not in str(pipeline)
    assert "$toDecimal" in str(pipeline)
    assert "Decimal128('10000')" in str(pipeline)


def test_health_reports_unavailable_mongodb_as_503():
    with patch.object(
        main,
        "ping_mongodb",
        side_effect=ServerSelectionTimeoutError("MongoDB is offline"),
    ):
        with pytest.raises(HTTPException) as error:
            main.health()

    assert error.value.status_code == 503


def test_health_initializes_indexes_after_mongodb_recovers():
    with (
        patch.object(main, "_indexes_initialized", False),
        patch.object(main, "ping_mongodb"),
        patch.object(main, "create_required_indexes") as create_required_indexes,
    ):
        assert main.health() == {"status": "ok"}

    create_required_indexes.assert_called_once_with()


def test_application_starts_when_mongodb_is_temporarily_unavailable():
    async def enter_lifespan():
        async with main.lifespan(main.app):
            pass

    with patch.object(
        main,
        "create_required_indexes",
        side_effect=ServerSelectionTimeoutError("MongoDB is offline"),
    ):
        asyncio.run(enter_lifespan())


def test_history_lock_matches_missing_or_empty_history():
    assert build_history_length_filter([]) == {
        "$or": [
            {"history": {"$size": 0}},
            {"history": {"$exists": False}},
        ]
    }
    assert build_history_length_filter([{"by": "hr.admin"}]) == {"history": {"$size": 1}}


def test_calendar_date_rejects_impossible_date():
    with pytest.raises(HTTPException) as error:
        parse_calendar_date("2026-02-30", "date_from")

    assert error.value.status_code == 422