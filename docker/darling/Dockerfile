# Builds the Darling collector service for linux-x64 from the tagged source archive.
# Upstream publishes Windows artifacts only (issue #1804); swap the build stage for a pull
# when a linux-x64 image ships. Only the Service project is built - the WPF Viewer is not
# referenced and does not build on Linux.
ARG DOTNET_SDK_TAG=10.0
ARG DOTNET_RUNTIME_TAG=10.0

FROM mcr.microsoft.com/dotnet/sdk:${DOTNET_SDK_TAG} AS build
# Keep in step with perfmon_version in roles/perfmon_install/defaults/main.yml.
ARG PERFMON_VERSION=v3.3.0
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o src.zip \
       "https://github.com/erikdarlingdata/PerformanceMonitor/archive/refs/tags/${PERFMON_VERSION}.zip" \
    && unzip -q src.zip \
    && rm src.zip \
    && mv "PerformanceMonitor-${PERFMON_VERSION#v}" upstream
WORKDIR /src/upstream
RUN dotnet publish Darling/PerformanceMonitor.Darling.Service/PerformanceMonitor.Darling.Service.csproj \
      -c Release -r linux-x64 --self-contained false -o /app

FROM mcr.microsoft.com/dotnet/aspnet:${DOTNET_RUNTIME_TAG}
# libgssapi-krb5-2 for `auth: integrated`; SqlClient loads it lazily, so without it
# integrated auth fails at connect rather than at startup. gettext-base supplies envsubst.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgssapi-krb5-2 gettext-base \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
