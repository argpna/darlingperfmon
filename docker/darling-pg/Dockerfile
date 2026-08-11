FROM timescale/timescaledb:2.24.0-pg17

# darling_gunzip() needs plpython3u to read query_plan_gz - the collector has stored plan
# XML gzip-compressed there since v3.4.0/#2069, and the plpython3u extension's control
# files ship in the base image but its Python runtime does not. Upstream tracking: #2071
RUN apk add --no-cache postgresql17-plpython3
