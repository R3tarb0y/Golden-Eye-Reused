# Golden Eye repository security

**Create GitHub and GitLab repositories as Private.** This project contains business-specific audit logic. Confirm company permissions before sharing source, network details, estate names, SQL schemas or logos with external users.

- Keep `.env`, PATs, SSH keys, database dumps, Excel files, OPH exports, photos, embeddings, NAS mirrors, and report outputs outside Git.
- `.gitignore` prevents new accidental additions but does not remove sensitive files previously committed. Use a new clean repository/history or coordinate history rewriting with your Git admins.
- The included source templates are not a secret scanner substitute. Always manually review code, migrations, documentation, and Git diff before publishing.
- Store credentials in per-host environment files or a secrets manager. Use least-privileged credentials for development.
- If a credential was committed, **revoke/rotate it immediately**, then coordinate removal from Git history and both Git providers.
- Dashboard branding and code may be proprietary. Keep repositories Private until approved for public release.
