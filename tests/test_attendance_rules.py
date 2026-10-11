import pytest
from starlette.testclient import TestClient
from app.main import (
    app, db, create_required_indexes,
    calculate_late_minutes, calculate_overtime_minutes,
    calculate_work_hours_and_half_day, attendance_date_from_punch_in,
    count_monthly_workdays, IST, datetime_to_epoch_milliseconds, epoch_milliseconds_to_utc_datetime
)
from datetime import datetime, timezone, timedelta
import pathlib
from bson import json_util

client = TestClient(app)

def setup_module():
    create_required_indexes()
    project_root = pathlib.Path(__file__).resolve().parents[1]
    sample_data_directory = project_root / "sample_data"
    if not sample_data_directory.is_dir():
        sample_data_directory = project_root.parent / "SDT_candidate_kit" / "candidate_kit" / "sample_data"
    json_options = json_util.JSONOptions(tz_aware=True)
    for collection_name in ("employees", "attendance_logs"):
        sample_documents = json_util.loads((sample_data_directory / f"{collection_name}.json").read_text(), json_options=json_options)
        db[collection_name].delete_many({})
        db[collection_name].insert_many(sample_documents)

def test_rule_r1_overnight_shift_date():
    # Overnight shift: 22:00 - 06:00
    # Punch in 2026-07-07 01:15 IST (strictly before 06:00) -> belongs to 2026-07-06
    early_overnight_punch_in = datetime(2026, 7, 7, 1, 15, 0, tzinfo=IST).astimezone(timezone.utc)
    early_shift_attendance_date = attendance_date_from_punch_in(early_overnight_punch_in, "22:00", "06:00")
    assert early_shift_attendance_date == "2026-07-06"

    # Punch in 2026-07-06 21:55 IST (not before 06:00) -> belongs to 2026-07-06
    evening_overnight_punch_in = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST).astimezone(timezone.utc)
    evening_shift_attendance_date = attendance_date_from_punch_in(evening_overnight_punch_in, "22:00", "06:00")
    assert evening_shift_attendance_date == "2026-07-06"

    # Normal shift: 09:30 - 18:30 on 2026-07-07 09:20 IST -> 2026-07-07
    day_shift_punch_in = datetime(2026, 7, 7, 9, 20, 0, tzinfo=IST).astimezone(timezone.utc)
    day_shift_attendance_date = attendance_date_from_punch_in(day_shift_punch_in, "09:30", "18:30")
    assert day_shift_attendance_date == "2026-07-07"

def test_rule_r2_late_minutes_examples():
    # Shift 09:30:
    # 09:40:00 -> 0
    punch_at_grace_limit = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST).astimezone(timezone.utc)
    assert calculate_late_minutes(punch_at_grace_limit, "09:30", "2026-07-06") == 0

    # 09:40:01 -> 10
    punch_one_second_past_grace = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST).astimezone(timezone.utc)
    assert calculate_late_minutes(punch_one_second_past_grace, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45
    punch_forty_five_minutes_late = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST).astimezone(timezone.utc)
    assert calculate_late_minutes(punch_forty_five_minutes_late, "09:30", "2026-07-06") == 45

def test_rule_r3_overtime_threshold():
    # Normal shift 09:30 - 18:30 on 2026-07-06
    # Punch out 18:59 (29 min OT) -> 0
    punch_twenty_nine_minutes_overtime = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST).astimezone(timezone.utc)
    assert calculate_overtime_minutes(punch_twenty_nine_minutes_overtime, "09:30", "18:30", "2026-07-06") == 0

    # Punch out 19:00 (30 min OT) -> 30
    punch_thirty_minutes_overtime = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    assert calculate_overtime_minutes(punch_thirty_minutes_overtime, "09:30", "18:30", "2026-07-06") == 30

    # Overnight shift 22:00 - 06:00 on 2026-07-06
    # Punch out 2026-07-07 06:40 (40 min OT) -> 40
    overnight_shift_punch_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST).astimezone(timezone.utc)
    assert calculate_overtime_minutes(overnight_shift_punch_out, "22:00", "06:00", "2026-07-06") == 40

def test_rule_r4_and_r5_work_hours_half_day():
    # 2.125 hours = 7650 seconds -> half up to 2 decimals = 2.13 (Banker's would do 2.12)
    punch_in_datetime = datetime(2026, 7, 6, 9, 0, 0, tzinfo=timezone.utc)
    punch_out_after_two_hours = punch_in_datetime + timedelta(seconds=7650)
    rounded_work_hours, half_day = calculate_work_hours_and_half_day(punch_in_datetime, punch_out_after_two_hours)
    assert rounded_work_hours == 2.13
    assert half_day is True # < 4.50

    # Exactly 4.50 hours = 16200 seconds -> half_day is False
    punch_out_after_four_and_half_hours = punch_in_datetime + timedelta(seconds=16200)
    rounded_work_hours_at_half_day_cutoff, half_day_45 = calculate_work_hours_and_half_day(punch_in_datetime, punch_out_after_four_and_half_hours)
    assert rounded_work_hours_at_half_day_cutoff == 4.50
    assert half_day_45 is False

def test_rule_r7_working_days_mid_month_joiner():
    # EMP0002 joined on 2026-07-13 (Monday).
    # Month is July 2026 (31 days).
    # From 2026-07-13 to 2026-07-31:
    # Weeks: July 13-17 (5), July 20-24 (5), July 27-31 (5) = 15 working days.
    assert count_monthly_workdays("2026-07-13", "2026-07") == 15
    # Full month joiner (joined 2026-01-05): 23 working days in July 2026.
    assert count_monthly_workdays("2026-01-05", "2026-07") == 23
    # Future joiner (joined 2026-08-01): 0 working days in July 2026.
    assert count_monthly_workdays("2026-08-01", "2026-07") == 0

def test_regularize_absent_clearing_punches():
    # Regularize EMP0001 on 2026-07-06 to ABSENT
    regularization_response = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Marked absent by HR correction",
        "regularized_by": "hr.lead"
    })
    assert regularization_response.status_code == 200
    regularized_attendance_data = regularization_response.json()
    assert regularized_attendance_data["status"] == "ABSENT"
    assert regularized_attendance_data["punch_in"] is None
    assert regularized_attendance_data["punch_out"] is None
    assert regularized_attendance_data["work_hours"] is None
    assert regularized_attendance_data["late_minutes"] == 0
    assert regularized_attendance_data["overtime_minutes"] == 0
    assert regularized_attendance_data["half_day"] is False
    assert len(regularized_attendance_data["history"]) == 1

if __name__ == "__main__":
    setup_module()
    test_rule_r1_overnight_shift_date()
    test_rule_r2_late_minutes_examples()
    test_rule_r3_overtime_threshold()
    test_rule_r4_and_r5_work_hours_half_day()
    test_rule_r7_working_days_mid_month_joiner()
    test_regularize_absent_clearing_punches()
    print("ALL EDGE CASE TESTS PASSED!")
