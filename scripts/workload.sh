#!/bin/bash
# Demo workload against $TARGET_HOST to populate the dashboards.
#
# Structured as independent lanes backgrounded from main(), each on its own connection
# and cadence. Lanes A-C stay tight and steady so their plans survive collector polls;
# Lanes D-F are free to be disruptive since they no longer share a cycle with anything
# that needs a stable plan.
#
# Lane A: steady-state proc/query core (query_stats, procedure_stats, query heatmap).
# Lane B: one fixed slow query, clears the Long Queries XE session's 2s threshold.
# Lane C: periodic index drop/recreate - isolated Query Store cost regression.
# Lane D: blocking pair + periodic deadlock pair.
# Lane E: server/trace-flag config toggles + severe error (non-cache-clearing).
# Lane F: rare chaos - MAXDOP + cost threshold for parallelism toggles, FREEPROCCACHE, DDL churn.
set -u

SQLCMD=/opt/mssql-tools18/bin/sqlcmd
HOST=${TARGET_HOST:-mssql-2022}

q() { "$SQLCMD" -S "$HOST" -U sa -P "$MSSQL_SA_PASSWORD" -C -l 30 -t 120 "$@"; }
qr() { "$SQLCMD" -S "$HOST" -U workload_reader -P "$MSSQL_SA_PASSWORD" -C -l 30 -t 120 -H workload-core "$@"; }

echo "workload: waiting for $HOST"
until q -b -Q "SELECT 1" -o /dev/null; do sleep 5; done

setup() {
    q -d master -Q "
IF DB_ID(N'WorkloadDemo') IS NULL CREATE DATABASE WorkloadDemo;
"

    q -d WorkloadDemo -Q "
IF OBJECT_ID(N'dbo.orders') IS NULL
BEGIN
    CREATE TABLE dbo.orders (id integer PRIMARY KEY, customer integer NOT NULL, amount decimal(10,2) NOT NULL, note nvarchar(400));
    INSERT dbo.orders (id, customer, amount, note)
    SELECT TOP (50000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)), ABS(CHECKSUM(NEWID())) % 1000, ABS(CHECKSUM(NEWID())) % 10000 / 100.0, REPLICATE(N'x', 100)
    FROM sys.all_columns a CROSS JOIN sys.all_columns b;
END;
"

    # Extra rows for customer 42 skew that group heavily (~5000 rows vs ~55 for a typical
    # customer), which is what gives usp_order_report a real spread across heatmap buckets
    # depending on which customer is passed in, and a real index-dependent cost to regress.
    q -d WorkloadDemo -Q "
IF NOT EXISTS (SELECT 1 FROM dbo.orders WHERE id > 50000)
    INSERT dbo.orders (id, customer, amount, note)
    SELECT TOP (5000) 50000 + ROW_NUMBER() OVER (ORDER BY (SELECT NULL)), 42, ABS(CHECKSUM(NEWID())) % 5 / 1.0, REPLICATE(N'y', 100)
    FROM sys.all_columns a CROSS JOIN sys.all_columns b;
"

    q -d WorkloadDemo -Q "
CREATE OR ALTER PROCEDURE dbo.usp_customer_summary @min_amount decimal(10,2)
AS
    SELECT TOP (20) customer, total = SUM(amount), cnt = COUNT(*)
    FROM dbo.orders
    WHERE amount > @min_amount
    GROUP BY customer
    ORDER BY total DESC;
"

    q -d WorkloadDemo -Q "
CREATE OR ALTER PROCEDURE dbo.usp_order_scan @lo integer, @hi integer
AS
    SELECT id, customer, amount
    FROM dbo.orders
    WHERE id BETWEEN @lo AND @hi
    ORDER BY amount DESC;
"

    q -d WorkloadDemo -Q "
CREATE OR ALTER PROCEDURE dbo.usp_top_customers @top_n integer
AS
    SELECT TOP (@top_n) customer, order_count = COUNT(*), revenue = SUM(amount)
    FROM dbo.orders
    GROUP BY customer
    ORDER BY revenue DESC;
"

    q -d WorkloadDemo -Q "
CREATE OR ALTER PROCEDURE dbo.usp_get_order @id integer
AS
    SELECT id, customer, amount, note
    FROM dbo.orders
    WHERE id = @id;
"

    # Per-customer percentile scan: a correlated subquery over that customer's own rows.
    # Cost tracks the customer's row count - customer 42 (~5000 rows) shows ~500-900ms,
    # a typical customer (~55 rows) shows ~50-100ms - and ix_orders_customer_amount below
    # is what lets the optimizer avoid a full per-customer rescan for the correlated count.
    q -d WorkloadDemo -Q "
CREATE OR ALTER PROCEDURE dbo.usp_order_report @customer integer
AS
    SELECT o1.id, o1.amount,
        percentile = (SELECT COUNT(*) FROM dbo.orders AS o2 WHERE o2.customer = o1.customer AND o2.amount <= o1.amount)
    FROM dbo.orders AS o1
    WHERE o1.customer = @customer
    ORDER BY o1.amount DESC
    OPTION (MAXDOP 1);
"

    # Baseline healthy state for Lane C's regression cycle - see lane_regression.
    q -d WorkloadDemo -Q "
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_orders_customer_amount' AND object_id = OBJECT_ID(N'dbo.orders'))
    CREATE NONCLUSTERED INDEX ix_orders_customer_amount ON dbo.orders (customer, amount);
"

    # Dedicated low-priv login for Lane A.
    q -Q "
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'workload_reader')
    CREATE LOGIN workload_reader WITH PASSWORD = N'$MSSQL_SA_PASSWORD', CHECK_POLICY = OFF;
"
    q -d WorkloadDemo -Q "
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'workload_reader')
BEGIN
    CREATE USER workload_reader FOR LOGIN workload_reader;
    ALTER ROLE db_datareader ADD MEMBER workload_reader;
    GRANT EXECUTE ON SCHEMA::dbo TO workload_reader;
END;
"

    # Establish a known baseline for server configuration so the first workload toggle
    # registers as a real delta in config.server_configuration_history.
    # max server memory capped at 512 MB so memory pressure remains visible on dashboards.
    q -Q "
EXEC sp_configure 'show advanced options', 1;
RECONFIGURE;
EXEC sp_configure 'max server memory (MB)', 512;
EXEC sp_configure 'cost threshold for parallelism', 5;
EXEC sp_configure 'optimize for ad hoc workloads', 0;
RECONFIGURE;
" -o /dev/null 2>&1
}

# Steady, cheap, stable-plan traffic: several distinct parameterized procs plus one ad
# hoc shape run via sp_executesql (so its plan/sql_handle is reused across parameter
# values instead of minting a fresh single-use plan per call). ~5s sleep between
# iterations means several executions of everything stay inside every 60s collector poll.
lane_core() {
    local i=0
    while true; do
        i=$((i + 1))

        local min_amount
        case $((i % 3)) in
            0) min_amount=0.01 ;;
            1) min_amount=50.00 ;;
            *) min_amount=500.00 ;;
        esac
        qr -d WorkloadDemo -Q "EXEC dbo.usp_customer_summary @min_amount = $min_amount;" -o /dev/null 2>&1
        qr -d WorkloadDemo -Q "EXEC dbo.usp_order_scan @lo = 1, @hi = 50000;" -o /dev/null 2>&1
        qr -d WorkloadDemo -Q "EXEC dbo.usp_order_scan @lo = 1, @hi = 500;" -o /dev/null 2>&1

        local customer
        case $((i % 3)) in
            0) customer=42 ;;
            1) customer=7 ;;
            *) customer=123 ;;
        esac
        qr -d WorkloadDemo -Q "EXEC dbo.usp_order_report @customer = $customer;" -o /dev/null 2>&1
        qr -d WorkloadDemo -Q "EXEC dbo.usp_top_customers @top_n = 50;" -o /dev/null 2>&1
        qr -d WorkloadDemo -Q "EXEC dbo.usp_top_customers @top_n = 10;" -o /dev/null 2>&1

        # PK-seek burst in one batch (one connection, 40 statements) - cheap, single-row
        # lookups that push delta_execution_count into the heatmap's higher count buckets
        # without 40 separate connection round-trips.
        local seeks="" id
        for id in $(seq 1 40); do
            seeks="${seeks}EXEC dbo.usp_get_order @id = $id;"$'\n'
        done
        qr -d WorkloadDemo -Q "$seeks" -o /dev/null 2>&1

        local p=$(( (i % 8) * 5 + 5 ))
        qr -d WorkloadDemo -Q "
EXEC sp_executesql N'SELECT c = COUNT_BIG(*) FROM dbo.orders o1 JOIN dbo.orders o2 ON o1.customer = o2.customer WHERE o1.amount > @p OPTION (MAXDOP 1);', N'@p decimal(10,2)', @p = $p.00;
SELECT TOP (100) customer, total = SUM(amount) FROM dbo.orders GROUP BY customer ORDER BY total DESC;
" -o /dev/null 2>&1

        sleep 5
    done
}

# One fixed, parameterized, ~6s query - clears the Long Queries XE session's 2,000,000us
# default duration threshold with margin, and is a stable-shaped row in query_stats/
# query_store_stats/the heatmap's 1-10s bucket in its own right, independent of whether
# long_query_completions is enabled.
lane_slow() {
    while true; do
        q -d WorkloadDemo -Q "
EXEC sp_executesql N'
SELECT TOP (200) o1.id,
    related = (SELECT COUNT(*) FROM dbo.orders AS o3 WHERE o3.customer = o1.customer AND o3.amount > @p AND o3.note LIKE N''%xyz%'')
FROM dbo.orders AS o1
JOIN dbo.orders AS o2 ON o2.customer = o1.customer AND o2.id <> o1.id
WHERE o1.amount > @p
ORDER BY o1.amount DESC
OPTION (MAXDOP 1);', N'@p decimal(10,2)', @p = 2.00;
" -o /dev/null 2>&1
        sleep 25
    done
}

# Query Store regression generator: drop the index usp_order_report(@customer = 42) relies
# on for a while, then recreate it.
lane_regression() {
    while true; do
        sleep 1200
        q -d WorkloadDemo -Q "DROP INDEX IF EXISTS ix_orders_customer_amount ON dbo.orders;" -o /dev/null 2>&1
        sleep 600
        q -d WorkloadDemo -Q "CREATE NONCLUSTERED INDEX ix_orders_customer_amount ON dbo.orders (customer, amount);" -o /dev/null 2>&1
    done
}

# Blocking pair: holder keeps a row locked 40s, victim waits on it. Deadlock pair every
# 3rd cycle.
lane_blocking() {
    local cycle=0
    while true; do
        cycle=$((cycle + 1))

        q -d WorkloadDemo -Q "
BEGIN TRAN;
UPDATE dbo.orders SET amount = amount WHERE id = 1;
WAITFOR DELAY '00:00:40';
ROLLBACK;
" -o /dev/null 2>&1 &
        sleep 3
        q -d WorkloadDemo -Q "
SET LOCK_TIMEOUT 30000;
UPDATE dbo.orders SET note = note WHERE id = 1;
" -o /dev/null 2>&1 &

        if [ $((cycle % 3)) = 0 ]; then
            q -d WorkloadDemo -Q "
BEGIN TRAN;
UPDATE dbo.orders SET amount = amount WHERE id = 100;
WAITFOR DELAY '00:00:05';
UPDATE dbo.orders SET amount = amount WHERE id = 200;
ROLLBACK;
" -o /dev/null 2>&1 &
            q -d WorkloadDemo -Q "
BEGIN TRAN;
UPDATE dbo.orders SET amount = amount WHERE id = 200;
WAITFOR DELAY '00:00:05';
UPDATE dbo.orders SET amount = amount WHERE id = 100;
ROLLBACK;
" -o /dev/null 2>&1 &
        fi

        wait
        sleep 10
    done
}

# Server config / trace flag toggles + severe error. Only 'optimize for ad hoc workloads'
# is toggled here since it doesn't clear the plan cache. 'cost threshold for parallelism'
# forces a full plan cache flush on RECONFIGURE (a cached plan's parallelism choice would
# otherwise contradict the new threshold), so it lives in lane_chaos with the other
# cache-clearing events instead - toggling it on this lane's moderate cadence would reset
# lane A/B/C's cumulative plan-cache counters between collector polls.
lane_config_churn() {
    local cycle=0
    while true; do
        cycle=$((cycle + 1))

        # optimize for ad hoc workloads: the collector is scheduled daily
        # (frequency_minutes=1440), so invoke it immediately after each change instead of
        # relying on the SQL Agent schedule to catch the transition.
        if [ $((cycle % 4)) -eq 1 ]; then
            q -Q "EXEC sp_configure 'optimize for ad hoc workloads', 1; RECONFIGURE;" -o /dev/null 2>&1
            q -d PerformanceMonitor -Q "EXEC collect.server_configuration_collector;" -o /dev/null 2>&1
        elif [ $((cycle % 4)) -eq 3 ]; then
            q -Q "EXEC sp_configure 'optimize for ad hoc workloads', 0; RECONFIGURE;" -o /dev/null 2>&1
            q -d PerformanceMonitor -Q "EXEC collect.server_configuration_collector;" -o /dev/null 2>&1
        fi

        if [ $((cycle % 7)) -eq 1 ]; then
            q -Q "DBCC TRACEON(1222, -1); DBCC TRACEON(3604, -1);" -o /dev/null 2>&1
            q -d PerformanceMonitor -Q "EXEC collect.server_configuration_collector;" -o /dev/null 2>&1
        elif [ $((cycle % 7)) -eq 5 ]; then
            q -Q "DBCC TRACEOFF(1222, -1); DBCC TRACEOFF(3604, -1);" -o /dev/null 2>&1
            q -d PerformanceMonitor -Q "EXEC collect.server_configuration_collector;" -o /dev/null 2>&1
        fi

        # Severe error: severity 20 WITH LOG writes to the SQL Server error log and to
        # the system_health XE session ring buffer, which sp_HealthParser reads to
        # populate collect.HealthParser_SevereErrors.
        if [ $((cycle % 9)) = 0 ]; then
            q -Q "RAISERROR(N'PerfMon demo: simulated severe error for dashboard validation', 20, 1) WITH LOG;" -o /dev/null 2>&1
        fi

        sleep 60
    done
}

# Rare, deliberately disruptive events: DBCC FREEPROCCACHE, the database-scoped MAXDOP
# toggle and the server-scoped cost threshold for parallelism toggle (both clear plan
# cache on every change), and DDL churn. Isolated to a ~35 minute cadence so the
# steady-state lanes get long, undisturbed stretches between them.
lane_chaos() {
    local toggle=0
    while true; do
        sleep 2100

        toggle=$((1 - toggle))
        if [ "$toggle" -eq 1 ]; then
            q -d master -Q "ALTER DATABASE WorkloadDemo SET AUTO_UPDATE_STATISTICS OFF;" -o /dev/null 2>&1
            q -d WorkloadDemo -Q "ALTER DATABASE SCOPED CONFIGURATION SET MAXDOP = 4;" -o /dev/null 2>&1
            q -Q "EXEC sp_configure 'cost threshold for parallelism', 50; RECONFIGURE;" -o /dev/null 2>&1
        else
            q -d master -Q "ALTER DATABASE WorkloadDemo SET AUTO_UPDATE_STATISTICS ON;" -o /dev/null 2>&1
            q -d WorkloadDemo -Q "ALTER DATABASE SCOPED CONFIGURATION SET MAXDOP = 0;" -o /dev/null 2>&1
            q -Q "EXEC sp_configure 'cost threshold for parallelism', 5; RECONFIGURE;" -o /dev/null 2>&1
        fi
        q -d PerformanceMonitor -Q "EXEC collect.database_configuration_collector;" -o /dev/null 2>&1
        q -d PerformanceMonitor -Q "EXEC collect.server_configuration_collector;" -o /dev/null 2>&1

        # DDL events: generates Object:Created, Object:Altered, Object:Deleted in the default trace
        q -d WorkloadDemo -Q "
IF OBJECT_ID(N'dbo.demo_staging') IS NOT NULL DROP TABLE dbo.demo_staging;
CREATE TABLE dbo.demo_staging (id integer PRIMARY KEY, val decimal(10,2), loaded_at datetime2(0) DEFAULT SYSDATETIME());
ALTER TABLE dbo.demo_staging ADD note nvarchar(200) NULL;
DROP TABLE dbo.demo_staging;
" -o /dev/null 2>&1

        q -Q "DBCC FREEPROCCACHE;" -o /dev/null 2>&1
    done
}

setup

lane_core &
lane_slow &
lane_regression &
lane_blocking &
lane_config_churn &
lane_chaos &

wait
