# Create a new Gmail app password, then set it only in the current terminal.
$env:AI_MONITOR_SMTP_PASSWORD = "replace-with-new-app-password"

# Optional production secret. Use at least 32 random characters.
$env:AI_MONITOR_SECRET_KEY = "replace-with-a-long-random-secret"

# Start the production server from this same terminal:
python run_server.py
