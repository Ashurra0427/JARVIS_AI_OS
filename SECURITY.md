# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in JARVIS AI OS, please report it responsibly:

**DO NOT** open a public GitHub issue for security vulnerabilities.

Instead, please email **security@jarvis-ai-os.local** with:
- A description of the vulnerability
- Steps to reproduce
- Potential impact assessment
- Suggested fix (if available)

You should receive a response within 48 hours. If the vulnerability is accepted, we will work with you to coordinate a public disclosure timeline.

## Security Considerations

JARVIS AI OS has a broad attack surface by design. Key areas to be aware of:

### Shell Execution
The `actions/terminal/terminal_manager.py` executes shell commands. While guarded by `actions/security/`, any bypass could lead to arbitrary code execution.

### Browser Automation
Playwright-driven browser control can interact with any web content. Ensure the security policy engine restricts access to sensitive sites.

### API Keys and Credentials
All API keys should be stored in `.env` (git-ignored) and never hardcoded. Rotate keys immediately if leaked.

### File System Access
The filesystem tools can read/write/delete files. Permission enforcement happens in `actions/security/` — review changes there carefully.

### Network Exposure
The FastAPI server exposes endpoints over WebSocket. Deploy behind a reverse proxy with authentication in production.

## Best Practices

1. Never commit secrets or API keys
2. Run `make lint && make test-quick` before pushing
3. Review security-related changes in `actions/security/` with extra scrutiny
4. Use `JARVIS_SYSTEM__ENVIRONMENT=production` in production deployments
5. Keep Python at 3.11 or 3.12 (3.13 removed `audioop`)
