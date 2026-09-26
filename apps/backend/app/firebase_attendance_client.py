"""Phase 23 "Team management" (added 2026-09-26, per Sudeep's own scoping
choice - "pull in real attendance data" - see progress-tracker.md for the
full 3-question scoping discussion). A READ-ONLY wrapper around the
SEPARATE SS Retail Attendance app's Firebase project (a standalone React/
TypeScript/Firebase app Sudeep built independently -
github.com/kumarsudip3535-a11y/ss-retail-attendance, deployed at
https://ssretailattandance.netlify.app/ - NOT part of this codebase and
NOT modified by this feature). This module only ever reads the
`employees`/`attendanceLogs` Firestore collections that app already
maintains; it never writes to them, and it never touches that app's own
Firebase Auth/geofencing/live-tracking/payroll pieces at all.

Uses the official `firebase-admin` Python SDK rather than this project's
usual "hand-roll a simple REST integration" convention (see tally_client.py/
google_client.py/phone_agent.py) - a deliberate exception, because
authenticating as a Firebase service account means signing and exchanging
a real Google-issued JWT, which is exactly the kind of cryptographic
plumbing a maintained library should handle rather than this project
reimplementing it by hand.

Setup Sudeep needs to do himself, once, for this feature to actually turn
on (see .env.example for the exact settings): open the Firebase console for
the ss-retail-attendance project -> Project Settings -> Service Accounts ->
"Generate new private key" -> save the downloaded JSON file somewhere on
this machine -> set FIREBASE_ATTENDANCE_CREDENTIALS_PATH in .env to that
file's path. Until that's done, firebase_attendance_configured() returns
False and every caller (team_workload_report(), see chat_routes.py) says
plainly that attendance isn't connected rather than guessing - the same
"never invent a business fact when the real source isn't reachable"
principle as every other read integration in this project (Tally daybook,
Google Calendar, the reminder engine)."""
import datetime
import os

from app.config import settings


def firebase_attendance_configured() -> bool:
    """True only when a credentials file path is actually set AND the file
    is actually present on disk - checked fresh every call, never cached,
    so a missing/misconfigured key is caught immediately rather than after
    a confusing failure deeper in firebase-admin's own init code."""
    path = settings.firebase_attendance_credentials_path.strip()
    return bool(path) and os.path.isfile(path)


_firebase_app = None


def _get_firestore_client():
    """Lazily initializes exactly one firebase_admin App for this
    project's service account and returns a Firestore client bound to it.
    Guards against firebase_admin's own "app already initialized" error if
    this is called more than once in the same process (every previous
    integration in this project - Tally, Google, Twilio - has hit some
    variant of "don't re-initialize a client that's meant to be a
    singleton", so this follows the same pattern)."""
    global _firebase_app
    import firebase_admin
    from firebase_admin import credentials, firestore

    if _firebase_app is None:
        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
        else:
            cred = credentials.Certificate(settings.firebase_attendance_credentials_path)
            _firebase_app = firebase_admin.initialize_app(cred, name="ss_retail_attendance")
    return firestore.client(app=_firebase_app)


def list_active_employees() -> list[dict]:
    """Reads the `employees` collection (see the SS Retail Attendance
    app's src/types/employee.ts for the real, authoritative field shapes -
    this module never guesses a field name). Returns only non-disabled
    employees, sorted by name. Never raises - a Firestore/auth problem
    comes back as an empty list, and the caller is responsible for telling
    Sudeep plainly that the read failed rather than reporting zero real
    employees as if that were the actual roster."""
    if not firebase_attendance_configured():
        return []
    try:
        db = _get_firestore_client()
        docs = db.collection("employees").stream()
        employees = []
        for doc in docs:
            data = doc.to_dict() or {}
            if data.get("disabled"):
                continue
            employees.append(
                {
                    "id": doc.id,
                    "employee_id": data.get("employeeId", doc.id),
                    "name": data.get("name", "(unnamed)"),
                    "phone": data.get("phone", ""),
                }
            )
        employees.sort(key=lambda e: e["name"].lower())
        return employees
    except Exception:
        return []


def get_attendance_for_date(target_date: datetime.date, tz_name: str) -> "dict[str, dict] | None":
    """Reads `attendanceLogs` for exactly one real local calendar day (see
    AttendanceLog in the SS Retail Attendance app's src/types/attendance.ts
    for the real field shapes) and returns a dict keyed by the log's own
    `employeeId` field -> {"logged_in", "logged_out", "login_time_local",
    "logout_time_local", "total_hours"}. The day boundary is computed for
    real in Python using the attendance app's own real timezone (never
    trusted to Firestore's own UTC storage or guessed) - the same "compute
    a date/time boundary deterministically, never leave it to chance"
    principle as phone_agent_timezone/google_client.py's calendar-timezone
    handling elsewhere in this project. If an employee has more than one
    log for the same day (not expected in normal use, but not assumed
    impossible either), the most recently started one wins.

    Returns None (never a guess, never an empty-but-successful-looking
    dict) if attendance isn't configured or the read itself fails, so the
    caller can tell those two cases apart from "configured, connected, and
    genuinely nobody logged in today"."""
    if not firebase_attendance_configured():
        return None
    try:
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = datetime.timezone.utc

        day_start_local = datetime.datetime.combine(target_date, datetime.time.min, tzinfo=tz)
        day_end_local = day_start_local + datetime.timedelta(days=1)

        db = _get_firestore_client()
        query = (
            db.collection("attendanceLogs")
            .where("loginTime", ">=", day_start_local)
            .where("loginTime", "<", day_end_local)
        )
        result: "dict[str, dict]" = {}
        for doc in query.stream():
            data = doc.to_dict() or {}
            employee_id = data.get("employeeId")
            if not employee_id:
                continue
            login_time = data.get("loginTime")
            existing = result.get(employee_id)
            if existing is not None and existing.get("_login_dt") is not None and login_time is not None:
                if login_time <= existing["_login_dt"]:
                    continue

            logout_time = data.get("logoutTime")
            result[employee_id] = {
                "_login_dt": login_time,
                "logged_in": True,
                "logged_out": bool(logout_time),
                "login_time_local": _format_local(login_time, tz),
                "logout_time_local": _format_local(logout_time, tz) if logout_time else None,
                "total_hours": data.get("totalHours"),
            }
        for row in result.values():
            row.pop("_login_dt", None)
        return result
    except Exception:
        return None


def _format_local(value, tz) -> "str | None":
    """Formats a Firestore Timestamp (already a real timezone-aware
    datetime by the time the Python SDK hands it back) into the attendance
    app's own real local timezone for display - never left as a bare UTC
    value Sudeep would have to mentally convert."""
    if value is None:
        return None
    try:
        return value.astimezone(tz).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return None
