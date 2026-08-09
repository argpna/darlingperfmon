ARG DOTNET_RUNTIME_TAG=10.0

FROM alpine:3.20 AS fetch
ARG PERFMON_VERSION=v3.4.0
WORKDIR /src
RUN apk add --no-cache curl \
    && version="${PERFMON_VERSION#v}" \
    && archive="PerformanceMonitorDarling-linux-x64-${version}.tar.gz" \
    && base_url="https://github.com/erikdarlingdata/PerformanceMonitor/releases/download/${PERFMON_VERSION}" \
    && curl -fsSL -O "${base_url}/${archive}" \
    && curl -fsSL -o SHA256SUMS-linux.txt "${base_url}/SHA256SUMS-linux.txt" \
    && grep -F -- "${archive}" SHA256SUMS-linux.txt | sha256sum -c - \
    && mkdir /app \
    && tar -xzf "${archive}" -C /app \
    && rm "${archive}" SHA256SUMS-linux.txt

FROM mcr.microsoft.com/dotnet/aspnet:${DOTNET_RUNTIME_TAG}
# libgssapi-krb5-2 for `auth: integrated`; SqlClient loads it lazily, so without it
# integrated auth fails at connect rather than at startup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=fetch /app .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
