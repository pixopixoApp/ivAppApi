# Store-review login

The production review identity is `app-review@pixopixo.com`. Its fixed six-digit
code is held in the private task output outside this repository, not in source
or the Android application. Only a salted PBKDF2 digest is configured on the API.

Reviewers select email login, enter the supplied email, tap **Send code**, and
enter the fixed code. Inbox access is unnecessary. The code can be reused across
devices and after logout, without an OTP expiry. Each successful login issues an
ordinary expiring session, with the existing session-count limit.

The account has completed its adult birthday information, has ordinary creator
access, and receives a one-time allocation to a balance of 100 Credits for
reviewing creation. Signing in never replenishes Credits. No administrator
permissions or special publication privileges are provided.

`APP_REVIEW_LOGIN_ENABLED`, `APP_REVIEW_LOGIN_EMAIL`, and
`APP_REVIEW_LOGIN_CODE_HASH` opt in exactly one pre-existing active email
identity. A fixed code cannot create another identity or authenticate another
email. Disabling the account still rejects login. Account-deletion codes remain
purpose-scoped ordinary one-time codes delivered by email.

To revoke the fixed login, set `APP_REVIEW_LOGIN_ENABLED=false` in production
and recreate the API using the production Compose profile. Existing sessions
continue until revoked/expired; disable the user and revoke its tokens if access
must stop immediately. Rotating the digest requires a new randomly generated
code and an API recreation. Keep the digest single-quoted in dotenv files.

The narrow release script backs up the affected login module and server dotenv
file on the server, checks the expected production source checksum, and changes
only the review module, verification module, provisioning script, and dedicated
configuration. It recreates only the API and rolls back source/configuration on
deployment failure. It does not sync unrelated local changes or database schemas.

Tests cover repeat login, mailbox independence, exact identity scoping, malformed
digests, disabled/missing identities, ordinary OTP expiry/consumption/cooldown,
account-deletion separation, and normal session issuance. Verification results
from the public production API are saved without access tokens or codes in
`output/pixo-store-review/verification.json` in the parent workspace.
