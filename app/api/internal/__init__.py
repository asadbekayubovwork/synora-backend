"""The surface the AI microservices call, not the one browsers call.

Mounted at `/internal/v1`, deliberately outside `settings.api_prefix`, so that
nginx can allowlist a whole path prefix at the edge:

    location /internal/ { allow <service-ip>; deny all; }

A path-prefix ACL is far harder to get wrong than a list of route names, and it
survives someone adding a route without remembering the ACL. `INTERNAL_API_ENABLED`
is a second switch that does not need a config reload on the proxy.
"""
