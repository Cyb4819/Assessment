# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, regularization audit trail, and analytics aggregation pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0)

## Quick Start
Use PowerShell from the `HROne Assessment` folder. The app reads `MONGO_URI` and `MONGO_DB` from `app/.env`; do not override them unless you intend to use a different database. Install dependencies once if they are not already installed:

```powershell
python -m pip install -r requirements.txt
```

To seed the supplied sample data into the configured database:

```powershell
python -c "from dotenv import load_dotenv; import runpy; load_dotenv('app/.env'); runpy.run_path(r'..\SDT_candidate_kit\candidate_kit\sample_data\sample_seed.py', run_name='__main__')"
```

The seeder replaces the `employees` and `attendance_logs` collections. Use a disposable database for sample data. Start the API with:

```powershell
python -m uvicorn app.main:app --port 8000
```

The application creates its indexes on startup. Open `http://127.0.0.1:8000/docs` for the interactive Swagger UI, where you can try the API endpoints. `http://127.0.0.1:8000/health` is the readiness check and should return `{"status":"ok"}` when MongoDB is connected. There is no separate custom frontend or dashboard.

## Key Implementation Highlights
- **Single-File Structure**: All models, business logic (R1–R10), routes, and aggregation pipelines are self-contained in `app/main.py`.
- **Concurrency & Races**: Race conditions on punch-in (`409 Conflict`) are enforced via a unique compound index on `(emp_code, date)`. Punch-out is guarded by atomic `find_one_and_update` on open records. Regularizations employ optimistic concurrency locking on the audit history.
- **Pure MongoDB Aggregations**: All analytics endpoints (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`) are fully executed in MongoDB without Python in-memory loops.
- **Index-Backed Performance**: Startup creates indexes intended to support attendance filtering, pagination, lookups, and analytics. Use `/admin/explain/{endpoint}` against the target MongoDB dataset to verify actual query plans.
- **Audit History**: Full immutable audit trail on `PATCH /attendance/{emp_code}/{date}` capturing field-level transitions.

## Verification & Tests
Run the automated checks with:

```powershell
python -m pytest -q
python -m tests.test_api_workflows
```

Both scripts initialize indexes and replace the sample collections in the configured database, so run them against a disposable database.
