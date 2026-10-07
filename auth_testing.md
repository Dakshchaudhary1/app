# Emergent Google Auth Testing Checklist

1. Create a test user and session in the configured MongoDB database, using a custom `user_id` and a session expiring in seven days.
2. Verify `GET /api/auth/me` with the session token in the Authorization header.
3. Verify protected admin endpoints with the same token.
4. Browser-test the OAuth callback using a URL fragment containing `session_id`, ensuring the callback is handled before protected-route checks.
5. Confirm the session cookie is httpOnly, secure, SameSite=None, and that logout clears it.
6. Ensure all MongoDB responses exclude `_id` and use custom UUID-style IDs.
7. Record allowed Google test identities and mapped roles in `/app/memory/test_credentials.md`.