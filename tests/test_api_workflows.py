import pathlib
from bson import json_util
from starlette.testclient import TestClient
from app.main import app, db, create_required_indexes

client = TestClient(app)

def seed_sample_database():
    project_root = pathlib.Path(__file__).resolve().parents[1]
    sample_data_directory = project_root / "sample_data"
    if not sample_data_directory.is_dir():
        sample_data_directory = project_root.parent / "SDT_candidate_kit" / "candidate_kit" / "sample_data"
    json_options = json_util.JSONOptions(tz_aware=True)
    for collection_name in ("employees", "attendance_logs"):
        sample_documents = json_util.loads((sample_data_directory / f"{collection_name}.json").read_text(), json_options=json_options)
        db[collection_name].delete_many({})
        db[collection_name].insert_many(sample_documents)

def run_api_workflow_tests():
    print("Initializing indexes and seeding DB...")
    create_required_indexes()
    seed_sample_database()

    print("Testing GET /health...")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    print("PASSED")

    print("Testing /employees...")
    # Validation failures
    invalid_employee_response = client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30", # equal shifts
        "joined_on": "2026-01-01"
    })
    assert invalid_employee_response.status_code == 422

    # Success create
    created_employee_response = client.post("/employees", json={
        "emp_code": "EMP0099",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2026-07-01"
    })
    assert created_employee_response.status_code == 201
    response_data = created_employee_response.json()
    assert response_data["emp_code"] == "EMP0099"
    assert isinstance(response_data["created_at"], int)
    assert "_id" not in response_data

    # Duplicate create -> 409
    duplicate_employee_response = client.post("/employees", json={
        "emp_code": "EMP0099",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "10:00",
        "shift_end": "19:00",
        "joined_on": "2026-07-01"
    })
    assert duplicate_employee_response.status_code == 409

    # List employees
    employee_list_response = client.get("/employees?department=QA&page=1&page_size=20")
    assert employee_list_response.status_code == 200
    employee_list_data = employee_list_response.json()
    assert employee_list_data["total"] == 1
    assert len(employee_list_data["items"]) == 1
    assert employee_list_data["items"][0]["emp_code"] == "EMP0099"
    print("PASSED")

    print("Testing /attendance/punch-in and punch-out...")
    punch_in_epoch_ms = 1783658100000 # 2026-07-10 04:35:00 UTC (10:05 IST)
    punch_in_response = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0099",
        "punched_at": punch_in_epoch_ms,
        "status": "PRESENT"
    })
    assert punch_in_response.status_code == 201
    punch_in_data = punch_in_response.json()
    assert punch_in_data["emp_code"] == "EMP0099"
    assert punch_in_data["date"] == "2026-07-10"
    assert punch_in_data["late_minutes"] == 0
    assert punch_in_data["punch_out"] is None

    # Duplicate punch in for same day -> 409
    duplicate_punch_in_response = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0099",
        "punched_at": punch_in_epoch_ms,
        "status": "PRESENT"
    })
    assert duplicate_punch_in_response.status_code == 409

    # Punch out 9 hours 35 min later
    punch_out_epoch_ms = punch_in_epoch_ms + 9 * 3600 * 1000 + 35 * 60 * 1000
    punch_out_response = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0099",
        "punched_at": punch_out_epoch_ms
    })
    assert punch_out_response.status_code == 200
    punch_out_data = punch_out_response.json()
    assert punch_out_data["work_hours"] == 9.58
    assert punch_out_data["overtime_minutes"] == 40
    assert punch_out_data["half_day"] is False

    # Second punch out -> 409
    duplicate_punch_out_response = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0099",
        "punched_at": punch_out_epoch_ms
    })
    assert duplicate_punch_out_response.status_code == 409
    print("PASSED")

    print("Testing /attendance regularization...")
    regularization_response = client.patch("/attendance/EMP0099/2026-07-10", json={
        "status": "WFH",
        "reason": "WFH approved by manager",
        "regularized_by": "manager.qa"
    })
    assert regularization_response.status_code == 200
    regularized_attendance = regularization_response.json()
    assert regularized_attendance["status"] == "WFH"
    assert len(regularized_attendance["history"]) == 1
    assert regularized_attendance["history"][0]["by"] == "manager.qa"
    assert regularized_attendance["history"][0]["reason"] == "WFH approved by manager"
    assert "status" in regularized_attendance["history"][0]["changes"]
    assert regularized_attendance["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}

    # No changes -> 422
    unchanged_regularization_response = client.patch("/attendance/EMP0099/2026-07-10", json={
        "status": "WFH",
        "reason": "Redundant update",
        "regularized_by": "manager.qa"
    })
    assert unchanged_regularization_response.status_code == 422
    print("PASSED")

    print("Testing GET /attendance list...")
    response = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-10")
    assert response.status_code == 200
    response_data = response.json()
    assert "items" in response_data
    assert "total" in response_data

    invalid_date_range_response = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-06")
    assert invalid_date_range_response.status_code == 422
    print("PASSED")

    print("Testing /analytics/employees/{emp_code}/monthly...")
    response = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["emp_code"] == "EMP0001"
    assert response_data["month"] == "2026-07"
    assert response_data["working_days"] == 23
    assert response_data["present_days"] == 2.0
    assert response_data["attendance_pct"] is not None
    print("PASSED")

    print("Testing /analytics/departments/summary...")
    response = client.get("/analytics/departments/summary?month=2026-07")
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["month"] == "2026-07"
    assert len(response_data["items"]) >= 3
    engineering_summary = next((department_result for department_result in response_data["items"] if department_result["department"] == "Engineering"), None)
    assert engineering_summary is not None
    assert engineering_summary["headcount"] == 2
    print("PASSED")

    print("Testing /analytics/leaderboard/late...")
    response = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert response.status_code == 200
    response_data = response.json()
    assert "items" in response_data
    assert len(response_data["items"]) >= 1
    assert response_data["items"][0]["rank"] == 1
    print("PASSED")

    print("Testing /analytics/departments/{department}/trend...")
    response = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-15")
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["department"] == "Engineering"
    assert len(response_data["items"]) == 10
    assert response_data["items"][0]["date"] == "2026-07-06"
    assert response_data["items"][-1]["date"] == "2026-07-15"
    print("PASSED")

    print("Testing /admin/explain/{endpoint}...")
    endpoints = [
        "/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-15",
    ]
    for url in endpoints:
        explain_response = client.get(url)
        assert explain_response.status_code == 200, f"Failed for {url}: {explain_response.text}"
        response_data = explain_response.json()
        assert "endpoint" in response_data
        assert "collection" in response_data
        assert "explain" in response_data
    print("PASSED")

    print("\nALL AUTOMATED TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    run_api_workflow_tests()
