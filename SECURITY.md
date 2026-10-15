# Security Policy

## Disclaimer and terms of use

Please read this before you run VoiceHive. It's short.

**Use it at your own risk.** This is beta software (v0.0.1-beta). It comes with no
warranty of any kind, and the author accepts no responsibility for anything that
happens because you ran it.

**It can get accounts banned.** VoiceHive drives Discord **user** accounts, which
Discord's Terms of Service do not allow. What that means in practice:

- Your account can be rate-limited, suspended, or permanently banned. That
  decision is entirely Discord's, and there is nothing here that can appeal it,
  reverse it, or soften it.
- It isn't necessarily limited to the account that ran the tool. Bans can also be
  applied by **IP / network** or by device, so other accounts on the same
  connection — **including your main account** — can get caught up in it even if
  you never used this tool on them.
- Which accounts you run this on, and what happens to them afterwards, is your
  decision and your responsibility.

**Use throwaway accounts.** Don't point this at an account you'd be upset to
lose.

**No warranty, no support obligation.** It may break at any time — for instance
if Discord changes the voice protocol. The repository itself could also be taken
down.

**Your tokens stay on your machine.** There is no server, no account, no
telemetry and no analytics in this project. Tokens are read from and written to
`accounts.json` in the project folder, in plain text, and they are sent only to
Discord when that account connects. Nothing is ever uploaded to the author or to
any third party. Delete the file and the tokens are gone.

**Not affiliated with Discord Inc.** Discord and the Discord logo are trademarks
of Discord Inc. This project is unofficial and unrelated to them.

By running VoiceHive, you accept all of the above.

---

## Supported versions

VoiceHive is a small project and only the latest commit on `main` is supported.
Fixes land there; there are no maintained release branches.

## Reporting a vulnerability

Please don't open a public issue for a security problem.

1. Go to the [Security tab](https://github.com/xavort/VoiceHive/security) of the
   repository.
2. Click **Report a vulnerability** and describe it there.

If that isn't possible, open a normal issue that says only *"security issue, I'd
like to report it privately"* — nothing more — and we'll move it somewhere
private.

It helps if you include:

- what the problem is and which file it's in;
- how to reproduce it;
- what an attacker could actually do with it.

I'll try to reply within a few days. This is a side project, so thanks in advance
for the patience.

## What's in scope

- Anything that leaks a token, whether to a log, a screenshot, a crash report, a
  network request or a file outside `accounts.json`.
- Anything that lets someone else control an account you added to the app.
- Crashes or code execution triggered by data coming back from Discord, or by a
  file the app is asked to open.

## What's out of scope

- The fact that `accounts.json` contains tokens in plain text. That's the design,
  and it's local-only. See the disclaimer above.
- Discord's own behaviour, including bans, rate limits and protocol changes.
- Bugs in `discord.py-self`, `davey` or libopus — report those upstream.
- Anything that requires an attacker to already have access to your machine or
  your `accounts.json`.

## If a token leaks

Treat it as compromised immediately:

1. Reset the account's token in Discord (changing the password invalidates
  existing sessions).
- Delete `accounts.json`, or remove just that entry.
- If it was pushed to a repository, remember that rewriting history isn't
  enough — assume the token is public and rotate it.
