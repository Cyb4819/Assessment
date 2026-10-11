"""
Employee Attendance & Analytics API

Main application file containing all endpoints, models, business rules,
and MongoDB aggregation pipelines as specified in the assignment contract.

Run:
    uvicorn app.main:app --port 8000
"""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import calendar
import json
import logging
import os
from typing import Any, Dict, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Environment & Database Configuration
# --------------------------------------------------------------------------- #
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "employee_attendance")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]
logger = logging.getLogger(__name__)
_indexes_initialized = False

IST = timezone(timedelta(hours=5, minutes=30))


def create_required_indexes():
    """Create all required indexes idempotently at startup."""
    global _indexes_initialized
    # employees collection
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    _indexes_initialized = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        create_required_indexes()
    except PyMongoError:
        logger.warning("MongoDB unavailable during startup; indexes will be retried by /health")
    yield


def ping_mongodb() -> None:
    client.admin.command("ping")


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Domain Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def datetime_to_epoch_milliseconds(datetime_value: Optional[datetime]) -> Optional[int]:
    """Convert a datetime instant to epoch milliseconds."""
    if datetime_value is None:
        return None
    if datetime_value.tzinfo is None:
        datetime_value = datetime_value.replace(tzinfo=timezone.utc)
    return int(datetime_value.timestamp() * 1000)


def epoch_milliseconds_to_utc_datetime(epoch_milliseconds: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    sec = epoch_milliseconds // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def round_number_half_up(number: Optional[float], decimal_places: int = 2) -> Optional[float]:
    """Round a float half-up to specified decimals (R8)."""
    if number is None:
        return None
    format_pattern = "0." + "0" * decimal_places if decimal_places > 0 else "0"
    return float(Decimal(str(number)).quantize(Decimal(format_pattern), rounding=ROUND_HALF_UP))


def attendance_date_from_punch_in(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started
    the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if shift_end <= shift_start:
        ist_time_str = punch_in_ist.strftime("%H:%M")
        if ist_time_str < shift_end:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def build_shift_boundary_datetime(attendance_date: str, shift_time: str, next_day: bool = False) -> datetime:
    """Build an IST datetime for a shift boundary on its attendance date."""
    shift_date = date.fromisoformat(attendance_date)
    if next_day:
        shift_date += timedelta(days=1)
    hour, minute = map(int, shift_time.split(":"))
    return datetime.combine(shift_date, datetime.min.time(), tzinfo=IST).replace(
        hour=hour, minute=minute
    )


def calculate_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2 Late.
    Grace period is 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start.
    Otherwise 0.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    shift_start_dt = build_shift_boundary_datetime(attendance_date, shift_start)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def calculate_overtime_minutes(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """
    R3 Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only if that is
    at least 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day (R1).
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    shift_end_dt = build_shift_boundary_datetime(
        attendance_date,
        shift_end,
        next_day=shift_end <= shift_start,
    )
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if elapsed_seconds > 0:
        minutes = int(elapsed_seconds // 60)
        if minutes >= 30:
            return minutes
    return 0


def calculate_work_hours_and_half_day(
    punch_in_utc: datetime, punch_out_utc: datetime
) -> tuple[float, bool]:
    """
    R4 Work hours: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    R5 Half day: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    exact_hours = Decimal(str(seconds)) / Decimal("3600")
    work_hours = float(exact_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    half_day = work_hours < 4.50
    return work_hours, half_day


def count_monthly_workdays(joined_on: str, year_month: str) -> int:
    """
    R7 Working days: Monday - Friday, no holiday calendar.
    For an employee who joined mid-month, working days are counted from joined_on.
    """
    year_number, month_number = map(int, year_month.split("-"))
    _, days_in_month = calendar.monthrange(year_number, month_number)
    month_start_date = datetime(year_number, month_number, 1).date()
    month_end_date = datetime(year_number, month_number, days_in_month).date()
    joined_date = datetime.fromisoformat(joined_on).date()

    if joined_date > month_end_date:
        return 0
    start_date = max(month_start_date, joined_date)

    working_day_count = 0
    current_date = start_date
    while current_date <= month_end_date:
        if current_date.weekday() < 5:  # Monday through Friday
            working_day_count += 1
        current_date += timedelta(days=1)
    return working_day_count


def parse_calendar_date(date_string: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(date_string)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid YYYY-MM-DD date")
    if parsed.isoformat() != date_string:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid YYYY-MM-DD date")
    return parsed


def build_history_length_filter(history: list) -> dict:
    if history:
        return {"history": {"$size": len(history)}}
    return {
        "$or": [
            {"history": {"$size": 0}},
            {"history": {"$exists": False}},
        ]
    }


# --------------------------------------------------------------------------- #
# Response Formatters
# --------------------------------------------------------------------------- #
def serialize_employee(employee_document: dict) -> dict:
    """Serialize MongoDB employee document for the API response."""
    return {
        "emp_code": employee_document["emp_code"],
        "name": employee_document["name"],
        "email": employee_document["email"],
        "department": employee_document["department"],
        "shift_start": employee_document["shift_start"],
        "shift_end": employee_document["shift_end"],
        "joined_on": employee_document["joined_on"],
        "created_at": datetime_to_epoch_milliseconds(employee_document["created_at"]),
    }


def serialize_attendance_record(attendance_document: dict) -> dict:
    """Serialize MongoDB attendance document for the API response."""
    history_list = []
    for audit_entry in attendance_document.get("history", []) or []:
        serialized_entry = dict(audit_entry)
        serialized_entry["at"] = datetime_to_epoch_milliseconds(serialized_entry.get("at"))
        original_changes = serialized_entry.get("changes", {})
        serialized_changes = {}
        for field_name, field_change in original_changes.items():
            if isinstance(field_change, dict):
                serialized_change = dict(field_change)
                if isinstance(serialized_change.get("from"), datetime):
                    serialized_change["from"] = datetime_to_epoch_milliseconds(serialized_change["from"])
                if isinstance(serialized_change.get("to"), datetime):
                    serialized_change["to"] = datetime_to_epoch_milliseconds(serialized_change["to"])
                serialized_changes[field_name] = serialized_change
            else:
                serialized_changes[field_name] = field_change
        serialized_entry["changes"] = serialized_changes
        history_list.append(serialized_entry)

    return {
        "emp_code": attendance_document["emp_code"],
        "date": attendance_document["date"],
        "status": attendance_document["status"],
        "punch_in": datetime_to_epoch_milliseconds(attendance_document.get("punch_in")),
        "punch_out": datetime_to_epoch_milliseconds(attendance_document.get("punch_out")),
        "work_hours": attendance_document.get("work_hours"),
        "late_minutes": attendance_document.get("late_minutes", 0),
        "overtime_minutes": attendance_document.get("overtime_minutes", 0),
        "half_day": bool(attendance_document.get("half_day", False)),
        "history": history_list,
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid date format, expected YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# 1. Health
@app.get("/health")
def health():
    try:
        ping_mongodb()
        if not _indexes_initialized:
            create_required_indexes()
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")


# 2. Employees: Create & List
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    employee_document = body.model_dump()
    employee_document["created_at"] = created_at
    try:
        db.employees.insert_one(employee_document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(employee_document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    employee_filter = {}
    if department:
        employee_filter["department"] = department
    total = db.employees.count_documents(employee_filter)
    records_to_skip = (page - 1) * page_size
    cursor = db.employees.find(employee_filter).sort("emp_code", 1).skip(records_to_skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# 3. Attendance: Punch-in, Punch-out, List & Regularize
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_milliseconds_to_utc_datetime(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = attendance_date_from_punch_in(punch_in_utc, employee["shift_start"], employee["shift_end"])
    late_minutes = calculate_late_minutes(punch_in_utc, employee["shift_start"], attendance_date)

    attendance_document = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(attendance_document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(attendance_document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_milliseconds_to_utc_datetime(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find employee's most recent record whose punch_in <= punched_at
    attendance_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )

    if not attendance_record:
        raise HTTPException(status_code=404, detail="No punch-in found for this punch-out")

    if attendance_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = attendance_record["punch_in"]
    if punch_out_utc <= punch_in_utc or (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours of it",
        )

    work_hours, half_day = calculate_work_hours_and_half_day(punch_in_utc, punch_out_utc)
    overtime_minutes = calculate_overtime_minutes(
        punch_out_utc, employee["shift_start"], employee["shift_end"], attendance_record["date"]
    )

    # Atomic update to guarantee single 200 on concurrent punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {"_id": attendance_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_calendar_date(date_from, "date_from")
    if date_to:
        parse_calendar_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    attendance_filter: Dict[str, Any] = {}
    if emp_code:
        attendance_filter["emp_code"] = emp_code
    if date_from or date_to:
        attendance_filter["date"] = {}
        if date_from:
            attendance_filter["date"]["$gte"] = date_from
        if date_to:
            attendance_filter["date"]["$lte"] = date_to
    if status:
        attendance_filter["status"] = status

    total = db.attendance_logs.count_documents(attendance_filter)
    records_to_skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(attendance_filter)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(records_to_skip)
        .limit(page_size)
    )

    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    parse_calendar_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    attendance_record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not attendance_record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # Status rules
    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch_in or punch_out when setting status ABSENT or LEAVE",
        )

    new_status = body.status if body.status is not None else attendance_record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in_utc = None
        new_punch_out_utc = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in_utc = epoch_milliseconds_to_utc_datetime(body.punch_in)
        else:
            new_punch_in_utc = attendance_record.get("punch_in")

        if new_punch_in_utc is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # R1 attendance date verification
        computed_date = attendance_date_from_punch_in(new_punch_in_utc, employee["shift_start"], employee["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {computed_date}, not {date}",
            )

        if body.punch_out is not None:
            new_punch_out_utc = epoch_milliseconds_to_utc_datetime(body.punch_out)
        else:
            new_punch_out_utc = attendance_record.get("punch_out")

        if new_punch_out_utc is not None:
            if new_punch_out_utc <= new_punch_in_utc:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out_utc - new_punch_in_utc).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be within 24 hours of punch_in",
                )
            new_work_hours, new_half_day = calculate_work_hours_and_half_day(
                new_punch_in_utc, new_punch_out_utc
            )
            new_overtime_minutes = calculate_overtime_minutes(
                new_punch_out_utc, employee["shift_start"], employee["shift_end"], date
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

        new_late_minutes = calculate_late_minutes(new_punch_in_utc, employee["shift_start"], date)

    # Calculate exact diff
    changes = {}
    if attendance_record["status"] != new_status:
        changes["status"] = {"from": attendance_record["status"], "to": new_status}
    if attendance_record.get("punch_in") != new_punch_in_utc:
        changes["punch_in"] = {"from": attendance_record.get("punch_in"), "to": new_punch_in_utc}
    if attendance_record.get("punch_out") != new_punch_out_utc:
        changes["punch_out"] = {"from": attendance_record.get("punch_out"), "to": new_punch_out_utc}
    if attendance_record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": attendance_record.get("work_hours"), "to": new_work_hours}
    if attendance_record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": attendance_record.get("late_minutes", 0), "to": new_late_minutes}
    if attendance_record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": attendance_record.get("overtime_minutes", 0),
            "to": new_overtime_minutes,
        }
    if bool(attendance_record.get("half_day", False)) != new_half_day:
        changes["half_day"] = {"from": bool(attendance_record.get("half_day", False)), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changed no fields")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking on history array size to avoid losing concurrent entries
    existing_history = attendance_record.get("history", []) or []
    history_lock = build_history_length_filter(existing_history)
    updated_record = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            **history_lock,
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in_utc,
                "punch_out": new_punch_out_utc,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_record:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance_record(updated_record)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #

# Helper: build employee_monthly aggregation pipeline
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    month_start = f"{month}-01"
    month_end = f"{month}-31"
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$project": {
                "present_contrib": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present_status"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                },
                "leave_contrib": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "late_contrib": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                "late_minutes": 1,
                "overtime_minutes": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_contrib"},
                "leave_days": {"$sum": "$leave_contrib"},
                "late_count": {"$sum": "$late_contrib"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_monthly_workdays(employee["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    aggregation_results = list(db.attendance_logs.aggregate(pipeline))

    if aggregation_results:
        monthly_totals = aggregation_results[0]
        present_days = float(monthly_totals.get("present_days", 0.0))
        leave_days = int(monthly_totals.get("leave_days", 0))
        late_count = int(monthly_totals.get("late_count", 0))
        total_late = int(monthly_totals.get("total_late_minutes", 0))
        total_ot = int(monthly_totals.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_number_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# Helper: build department_summary aggregation pipeline
def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    year, month_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, month_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "emp_code": 1,
                "department": 1,
                "is_log": {"$cond": [{"$ifNull": ["$logs._id", False]}, True, False]},
                "is_weekday": {
                    "$cond": [
                        {"$ifNull": ["$logs._id", False]},
                        {
                            "$in": [
                                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                [2, 3, 4, 5, 6],
                            ]
                        },
                        False,
                    ]
                },
                "is_present_status": {
                    "$cond": [
                        {"$ifNull": ["$logs._id", False]},
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        False,
                    ]
                },
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "work_hours": "$logs.work_hours",
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "status": "$logs.status",
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "department": 1,
                "present_contrib": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present_status"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                },
                "late_count_contrib": {
                    "$cond": [{"$and": ["$is_log", {"$gt": ["$late_minutes", 0]}]}, 1, 0]
                },
                "total_late_contrib": {"$cond": ["$is_log", "$late_minutes", 0]},
                "leave_contrib": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "on_duty_contrib": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                "eligible_work_hours": {
                    "$cond": [
                        {"$and": ["$is_present_status", {"$ne": ["$work_hours", None]}]},
                        "$work_hours",
                        "$$REMOVE",
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_set": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": "$present_contrib"},
                "late_count": {"$sum": "$late_count_contrib"},
                "total_late_minutes": {"$sum": "$total_late_contrib"},
                "leave_count": {"$sum": "$leave_contrib"},
                "on_duty_count": {"$sum": "$on_duty_contrib"},
                "avg_work_hours": {"$avg": "$eligible_work_hours"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_set"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)
    items = []
    for department_summary_row in cursor:
        items.append(
            {
                "department": department_summary_row["department"],
                "headcount": int(department_summary_row["headcount"]),
                "present_days": float(department_summary_row["present_days"]),
                "avg_work_hours": round_number_half_up(department_summary_row.get("avg_work_hours"), 2),
                "late_count": int(department_summary_row["late_count"]),
                "total_late_minutes": int(department_summary_row["total_late_minutes"]),
                "leave_count": int(department_summary_row["leave_count"]),
                "on_duty_count": int(department_summary_row["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


# Helper: build late_leaderboard aggregation pipeline
def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> list:
    year, month_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, month_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "matched_employee",
            }
        },
        {"$unwind": "$matched_employee"},
    ]

    if department:
        pipeline.append({"$match": {"matched_employee.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$matched_employee.name",
                    "department": "$matched_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = []
    for leaderboard_row in cursor:
        items.append(
            {
                "rank": int(leaderboard_row["rank"]),
                "emp_code": leaderboard_row["emp_code"],
                "name": leaderboard_row["name"],
                "department": leaderboard_row["department"],
                "total_late_minutes": int(leaderboard_row["total_late_minutes"]),
                "late_count": int(leaderboard_row["late_count"]),
            }
        )
    return {"month": month, "items": items}


# Helper: build department_trend aggregation pipeline
def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    range_start = datetime.fromisoformat(from_date)
    day_after_range_end = datetime.fromisoformat(to_date) + timedelta(days=1)
    scale = Decimal128("10000")
    rounding_bias = Decimal128("0.5")

    def round_half_up_expression(value: dict | str) -> dict:
        decimal_value = {"$toDecimal": value}
        return {
            "$divide": [
                {"$floor": {"$add": [{"$multiply": [decimal_value, scale]}, rounding_bias]}},
                scale,
            ]
        }

    return [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "matched_employee",
            }
        },
        {"$unwind": "$matched_employee"},
        {"$match": {"matched_employee.department": department}},
        {
            "$project": {
                "date": 1,
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            "$is_present",
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_as_datetime": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_as_datetime",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [range_start, day_after_range_end],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_as_datetime"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$trend_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "employee_count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.employee_count"}, 0]},
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_half_up_expression(
                            {"$divide": ["$present_count", "$headcount"]}
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        round_half_up_expression("$moving_avg_7d"),
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    d_from = parse_calendar_date(from_date, "from")
    d_to = parse_calendar_date(to_date, "to")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must not be earlier than from")

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = []
    for trend_row in cursor:
        items.append(
            {
                "date": trend_row["date"],
                "is_working_day": bool(trend_row["is_working_day"]),
                "headcount": int(trend_row["headcount"]),
                "present_count": float(trend_row["present_count"]),
                "late_count": int(trend_row["late_count"]),
                "attendance_rate": round_number_half_up(trend_row.get("attendance_rate"), 4),
                "moving_avg_7d": round_number_half_up(trend_row.get("moving_avg_7d"), 4),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(
        ...,
        pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    for name, value in (
        ("date_from", date_from),
        ("date_to", date_to),
        ("from", from_date),
        ("to", to_date),
    ):
        if value is not None:
            parse_calendar_date(value, name)

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        attendance_filter: Dict[str, Any] = {}
        if emp_code:
            attendance_filter["emp_code"] = emp_code
        if date_from or date_to:
            attendance_filter["date"] = {}
            if date_from:
                attendance_filter["date"]["$gte"] = date_from
            if date_to:
                attendance_filter["date"]["$lte"] = date_to
        if status:
            attendance_filter["status"] = status
        records_to_skip = (page - 1) * page_size
        explain_command = {
            "find": "attendance_logs",
            "filter": attendance_filter,
            "sort": {"date": -1, "emp_code": 1},
            "skip": records_to_skip,
            "limit": page_size,
        }
        collection = "attendance_logs"
        explain_result = db.command("explain", explain_command, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        collection = "attendance_logs"
        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", explain_command, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422, detail="month is required for department_summary"
            )
        pipeline = build_department_summary_pipeline(month, department)
        collection = "employees"
        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", explain_command, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422, detail="month is required for late_leaderboard"
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"
        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", explain_command, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required for department_trend",
            )
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to must not be earlier than from")
        d_from = datetime.fromisoformat(from_date).date()
        d_to = datetime.fromisoformat(to_date).date()
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        collection = "attendance_logs"
        explain_command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_result = db.command("explain", explain_command, verbosity="executionStats")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explain_result)),
    }
