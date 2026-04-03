## Summary
- wait for TikTok login toast to clear before scraping product data
- fail fast with a clear extraction error when the login toast never disappears
- document the restored login-wait business rule and add focused browser-flow tests

## Source
- tagged from main commit `commit-sha`

## Testing
- `.venv\Scripts\python.exe -m pytest`
