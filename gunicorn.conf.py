"""Gunicorn configuration for the Ranomics tools hub.

Forces preload so import errors surface in logs instead of silent worker
death. Railway dashboard start commands override Procfile, so
--preload lives here too.
"""

# Load app in master before forking workers.
preload_app = True

# Show worker lifecycle events (boot, exit, errors).
loglevel = "info"

# Log to stdout/stderr so Railway captures it.
accesslog = "-"
errorlog = "-"
