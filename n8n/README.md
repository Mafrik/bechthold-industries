# n8n
n8n is included in Docker; do not install it separately. Open http://localhost:5678 after startup.

Docker Compose pins n8n to the stable `2.43.7` release instead of following the moving `latest` tag. It disables the public n8n API, community packages, access to environment variables from Code nodes and expressions, telemetry, and version notifications. The web editor remains available. The app's workflow is imported with the n8n CLI and remains inactive in PAPER mode.

`My workflow.json` is the PAPER workflow imported into the local n8n instance. Its Strategy Engine defaults to `HOLD` if the daily change is missing, invalid, or below the +1% buy threshold. The workflow is intentionally inactive and uses a manual trigger. Validate its strategy code with `node --test n8n/test_workflow.js` from the repository root.
