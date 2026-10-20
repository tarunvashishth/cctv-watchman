# Contributing

Issues and pull requests are welcome.

- Run the tests before opening a PR: `cd notifier && python3 -m unittest test_tracker`
- Keep changes small and focused; add a test when you change `tracker.py` or `matcher.py`.
- Never commit `.env`, camera images, number plates, IPs or tokens. Check your diff for them.
- Don't add `hikcloud2rtc/` to the repo: it has no license, so users clone it themselves (see README, Cloud mode).
