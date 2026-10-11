# DECISIONS.md

1. **Indexes.**
   - On `employees`:
     - `{emp_code: 1}` (unique): guarantees business key uniqueness and powers O(1) lookups.
     - `{joined_on: 1, department: 1}` and `{department: 1, joined_on: 1}`: powers department filtering and period-cutoff headcount calculations in department summary and trend.
   - On `attendance_logs`:
     - `{emp_code: 1, date: 1}` (unique): guarantees at most one record per employee per attendance day, enforces punch-in idempotency, and accelerates employee date-range lookups.
     - `{date: -1, emp_code: 1}`: backs attendance list pagination without in-memory sorting, and supports date-range matching for department trend.
     - `{date: 1, late_minutes: 1, emp_code: 1}`: powers the late leaderboard aggregation with index-covered filtering.
     - `{emp_code: 1, punch_in: -1}`: optimizes the punch-out query to find an employee's most recent open record immediately.
   - *Considered and rejected*: An index on `status` alone; its low cardinality makes it inefficient for index selectivity compared to composite date indexes.

2. **Punch-in race.**
   Both concurrent requests validate payload and compute identical attendance dates. When both execute `db.attendance_logs.insert_one(...)`, MongoDB's unique compound index on `(emp_code, date)` guarantees atomic uniqueness at the storage engine level. One request succeeds and receives `201 Created`. The concurrent duplicate triggers a `DuplicateKeyError`, which our exception handler intercepts and maps directly to `409 Conflict`. No partial writes or duplicate records occur.

3. **Ties.**
   The leaderboard uses MongoDB's `$setWindowFields` stage with the `$rank` operator (standard competition ranking: 1, 2, 2, 4) followed by `rank <= limit`. If two employees tie on late minutes at the cutoff rank, both are returned (the response may contain more than `limit` rows). Ranking is calculated before limiting; omitting one tied employee would be arbitrary and violate competition ranking rules.

4. **Headcount.**
   The pipeline initiates from the `employees` collection (matching `joined_on <= period_end`), rather than `attendance_logs`. It then performs a `$lookup` into `attendance_logs` followed by `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with zero logs remain in the pipeline with null logs, contributing to department `headcount` via `$addToSet: "$emp_code"`, while contributing zero to attendance counts.

5. **One thing you would change for 100x data.**
   At 100x scale (~10 million logs), dynamic multi-collection lookups and large-window aggregations degrade latency. I would maintain pre-aggregated monthly summaries and daily department rollups (updated via MongoDB change streams or background cron workers) and shard `attendance_logs` on `{emp_code: 1, date: 1}` so that employee queries remain localized to single shards.
